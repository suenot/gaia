#!/usr/bin/env python3
"""Batch-generate the vertical (Shorts) frames for distance-approach-pairs-trading.

One Camoufox/Gemini session, prompts sent one after another into the same
thread; each prompt carries its desktop slide + style sample as image-to-image
references plus a blank 9:16 canvas that pins the portrait aspect. Frames land
in the video_maker temp dir as cover.png / slide_001..010.png / endcard.png.
"""
import asyncio
import shutil
import time
from pathlib import Path

from gemini_common import (
    load_cookies, log, shot, is_logged_in, make_camoufox,
    open_app, wait_for_editor, fill_prompt, send_message, attach_files,
)
from gemini_image_gen import JS_COLLECT_IMAGES, wait_for_images, _grab_one

GAIA = Path("/Users/suenot/projects/sdvg/gaia")
STYLE = Path("/Users/suenot/projects/sdvg/youtube_styles/style-library/buzz-hook-slide/samples/05-ink-theater.png")
ASPECT = GAIA / "assets/aspect-9x16.png"
DESKTOP = Path("/Users/suenot/projects/trading/marketmaker/video_maker/storyboards/distance-approach-pairs-trading/ink-theater-v3")
OUT = Path("/Users/suenot/projects/trading/marketmaker/video_maker/temp/distance-approach-pairs-trading_ink-theater-v3-vertical/frames")

COMMON = """Create one vertical 9:16 portrait technical video frame, 1080x1920, on pure white
tactile paper - recompose the attached 16:9 reference slide for portrait; do not crop,
letterbox, blur, or pad it. Keep every required text string verbatim, correct, and readable
at video size. Preserve at least 35% untouched negative space. Keep ALL critical content
(headline, mascot, diagrams, formulas, captions) inside the central safe area: horizontally
between 8% and 78% of the width, vertically between 9% and 75% of the height; the right edge
strip and the bottom quarter stay quiet empty paper with no text or key art (YouTube Shorts UI
overlay zone). Use confident variable-width black ink with subtle hand-drawn wobble. Include
exactly one deadpan solid-black mascot with tiny white-dot eyes; the mascot must physically
perform the metaphor on an absurd low-tech contraption. Strict color grammar: black = structure,
mascot, and type; orange = flow or arrows only; red = problem or warning only; blue =
successful end state only. Use crisp handwritten display lettering. Minimal, dry, intelligent,
never cute. No shading, hatching, 3D, gradients, UI cards, browser chrome, logos, random code,
decorative machinery, watermark, or "Gemini Notebook". Render only the listed text, verbatim
and exactly once."""

SLIDES = {
    "slide_001": ("narrated opening title scene", """Mascot at a drafting table measures two converging price paths with a divider compass; an orange arrow feeds both into one blue junction. Headline stacked in three lines: "The Architecture of Convergence:" / "Distance Approach" / "in Pairs Trading". Subline: "Mathematical formulation and Rust implementation for relative value statistical arbitrage." Red warning box, verbatim: "EDUCATIONAL ARTIFACT. The models, formulas, and code structures detailed herein are for educational analysis of statistical arbitrage mechanisms. They do not constitute investment advice. Trading real capital involves transaction costs, execution latency, and regime risks not captured in theoretical models."."""),
    "slide_002": ("Stage 1: Pair Formation Window", """Mascot holds a caliper measuring the gap between two hand-drawn normalized curves stacked one above the other on a tall white board; a blue bracket marks the formation window. Labels: "Stage 1: Pair Formation Window", "Normalized Scale [0.0-1.0]", "Asset A", "Asset B". Caption below the diagram: "Historical co-movement does not prove cointegration, but establishes a baseline geometry."."""),
    "slide_003": ("Distance & normalization", """Top panel: mascot cranks a mechanical adding machine that squares each gap between two zipped slices. Formula: "distance(X, Y) = sqrt(sum((x_i - y_i)^2))" with caption "Iterate over zipped slices, square the difference at each timestamp, and sum." Bottom panel: "Min-Max Normalization" with "norm_price = (price - min) / (max - min)" and red warning verbatim: "Calculate minimum and maximum using FORMATION WINDOW ONLY. Using future observations introduces look-ahead bias."."""),
    "slide_004": ("Universe Sorting & The Bounded Heap", """Mascot at the top shovels pair-tokens into a hopper feeding a tall bounded heap column; orange sorting arrows descend into it. Labels: "Universe Sorting & The Bounded Heap", "Universe size N", "O(N^2) pairwise combinations". Caption: "Keep only the closest pairs; the heap bounds memory."."""),
    "slide_005": ("Collapsing Geometry into Spread", """Mascot feeds two paths through a tall mangle press that flattens them into one spread line with a zero-line. Formula: "spread_t = norm_price_A_t - norm_price_B_t". Labels: "Formation Window", "Zero-Line". Captions: "Asset A is relatively expensive", "Asset A is relatively cheap"."""),
    "slide_006": ("Volatility-Based Signal Generation", """Mascot winds a round gauge with red upper/lower threshold marks and a blue zero-line; orange long/short arrows leave it downward. Formula: "Spread Volatility (sigma) = sqrt(average squared deviation from historical mean)" and "Threshold = sigma x Multiplier". Labels: "Upper Threshold", "Lower Threshold", "SHORT A / LONG B", "LONG A / SHORT B". Caption: "Validate thresholds via walk-forward testing. Do not select from reporting data."."""),
    "slide_007": ("Filters", """Mascot pours candidate tokens through two stacked sieve funnels arranged vertically: the first separates industry codes (Tech, Energy, Finance), the second counts zero crossings. Labels: "Filter 1: Industry Codes", "Filter 2: Zero Crossings", "Tech", "Energy", "Finance". Text: "Restrict candidates to economically related assets to improve logical plausibility. Require minimum sign changes to favor visible convergence/divergence history."."""),
    "slide_008": ("The Volatility Tradeoff", """Tall seesaw diagram: mascot balances between a red high-volatility end at the top and a red low-movement end at the bottom, with a blue "Goldilocks Zone" in the middle. Axis labels: "Y-Axis: Opportunity (Spread Variability / Std Dev)", "X-Axis: Relationship Stability (Historical Distance)". Captions: "Higher opportunity, but raises risk and weakens the assumption of relationship stability." and "Too little spread movement to cover trading costs (fees, bid-ask, borrow)." Caption: "Filter candidates by minimum spread-volatility, then rank by distance."."""),
    "slide_009": ("Distance vs. Pearson Approach Diagnostic", """Mascot at the top of a tall two-column comparison table holds two placards; a blue check marks the Distance column. Rows stacked vertically: "Input Data" (Distance: "Normalized Price Paths"; Pearson: "Correlation of returns"), "Core Math" (Distance: "Min-Max & Euclidean squared distance"; Pearson: "Pearson correlation"), "Implementation" (Distance: "Direct geometric comparison"; Pearson: "Statistic over paired series"), "Execution" (Distance: "Relative spread"; Pearson: "Spread > Z-score")."""),
    "slide_010": ("Rust implementation", """Mascot operates a tall steam-driven sorting engine built vertically: a SIMD gear block turning a bounded-heap hopper, with async belts feeding it. Labels: "TradingSignal Enum: Long, Short, Neutral states", "Bounded Heap", "SIMD Hot Loop", "Asynchronous Tasks", "Numerical Slices & Explicit Ownership". Captions: "Joining async results and sorting combined candidates." and "Processes multiple floating-point differences in one CPU operation for Euclidean distances." and "Processes independent ranges of the universe concurrently." Caption: "Optimization follows profiling: data loading and synchronization matter as much as arithmetic speed."."""),
    "slide_011": ("Execution Reality & System Validation", """Mascot walks a descending path through five red barrier gates stacked vertically: "Look-Ahead Bias", "Cost Friction", "Regime Shift", "Exogenous Shocks", "Risk Limits"; a blue exit gate at the bottom. Captions verbatim: "Must enforce strict barriers between formation and trading windows." "Backtests must deduct fees, bid-ask spread, market impact, and borrow costs." "Distance and past zero-crossings are not guarantees. Recalibrate or retire pairs when relationships break." "Monitor earnings, corporate actions, delistings, and sector-wide structural breaks." "Enforce maximum holding periods and position/pair/sector limits."."""),
    "endcard": ("silent end card, appended after narration", """Mascot gives a final bow next to a hand-cranked letterpress that stamps out a web address. Text: "Thank you for watching.", "Contacts: https://marketmaker.cc", "MORE QUANT RESEARCH". No "NEXT VIDEO" zone, no video frame placeholder, no watch-next block."""),
}


def desktop_ref(name: str) -> Path:
    """Vertical slide_00N maps to desktop slide-{N:02d}; endcard to slide-12."""
    if name == "endcard":
        return DESKTOP / "slide-12.png"
    return DESKTOP / f"slide-{int(name.split('_')[1]):02d}.png"


def make_prompt(name: str, spec) -> str:
    note, body = spec
    ref = "the attached reference slide"
    return (f"{COMMON}\n\n## Frame\n\n{body}\n\nLayout note: this is the {note}. "
            f"Recompose {ref} for the portrait safe area described above; "
            f"use the attached blank white 9:16 canvas only to pin the portrait "
            f"aspect ratio - it must stay plain white paper, do not render it.")


async def generate_one(page, name: str, spec) -> bool:
    slide = desktop_ref(name)
    refs = [slide if slide.exists() else STYLE, ASPECT]
    prompt = make_prompt(name, spec)

    log(f"[{name}] attaching {len(refs)} ref(s): {', '.join(r.name for r in refs)}")
    if not await attach_files(page, refs):
        log(f"[{name}] attach failed")
        await shot(page, f"vfail_{name}_attach")
        return False
    # Enter is a no-op while an attachment preview is still processing, and
    # shared wait_attachment over-counts old blob images from earlier turns.
    # Composer chips carry a Remove control - wait for exactly len(refs) of them.
    chips = 0
    for _ in range(40):
        chips = await page.locator("[aria-label*='Remove' i]").count()
        if chips >= len(refs):
            break
        await page.wait_for_timeout(500)
    log(f"[{name}] attachment chips: {chips}/{len(refs)}")
    await page.wait_for_timeout(1500)
    # Baseline AFTER the attachment previews settle: reference thumbnails
    # appearing in the DOM must not count as freshly generated images.
    await page.wait_for_timeout(2500)
    try:
        baseline = {im["src"] for im in await page.evaluate(JS_COLLECT_IMAGES)}
    except Exception:
        baseline = set()
    if not await fill_prompt(page, prompt) or not await send_message(page):
        log(f"[{name}] send failed")
        await shot(page, f"vfail_{name}_send")
        return False
    images = await wait_for_images(page, baseline, timeout_s=300, quiet_s=10)
    await shot(page, f"vok_{name}")
    # Only portrait results count; anything else is a reference echo.
    images = [im for im in images if im["h"] > im["w"]]
    if not images:
        log(f"[{name}] no portrait image returned")
        return False
    saved = OUT / f"{name}.png"
    data = await _grab_one(page, images[0])
    if data is None:
        log(f"[{name}] grab failed")
        return False
    saved.write_bytes(data)
    log(f"[{name}] saved {saved.name} ({len(data):,} bytes)")
    return True


async def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        _, cookies = load_cookies("default-release")
    except Exception as e:
        log(f"cookie extraction failed ({str(e).splitlines()[0][:60]}); relying on the persistent gaia profile")
        cookies = []
    failed = []
    async with make_camoufox(False) as context:
        page = await open_app(context, cookies)
        if not await is_logged_in(page):
            log("ERROR: not logged into Gemini")
            await shot(page, "vauth_fail")
            return 3
        editor, _sel = await wait_for_editor(page)
        if editor is None:
            log("ERROR: prompt editor not found")
            return 4
        for name, spec in SLIDES.items():
            target = OUT / f"{name}.png"
            if target.exists():
                log(f"[{name}] already exists, skip")
                continue
            ok = False
            for attempt in range(4):
                if await generate_one(page, name, spec):
                    ok = True
                    break
                log(f"[{name}] attempt {attempt+1} failed")
                await asyncio.sleep(8)
            if not ok:
                failed.append(name)
                break  # stop on first failure: inspect visually before more spend
    if failed:
        log(f"failed: {failed}")
        return 1
    # one title frame serves both the timeline slot and the poster role
    shutil.copyfile(OUT / "slide_001.png", OUT / "cover.png")
    log("ALL FRAMES DONE")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
