#!/usr/bin/env python3
"""Targeted text fixes for defective vertical Shorts frames.

Each entry attaches the CURRENT frame as the only reference and asks Gemini to
recreate it exactly, changing only the named text defect. This keeps the good
composition (full regen would throw it away) and pins the known Nano Banana
failure mode (duplicated word at a line-wrap) in the negative prompt.

Saves over the original file; back up the frames dir first if in doubt.
"""
import asyncio
from pathlib import Path

from gemini_common import (
    load_cookies, log, shot, is_logged_in, make_camoufox,
    open_app, wait_for_editor, fill_prompt, send_message, attach_files,
)
from gemini_image_gen import JS_COLLECT_IMAGES, wait_for_images, _grab_one

OUT = Path("/Users/suenot/projects/trading/marketmaker/video_maker/temp/distance-approach-pairs-trading_ink-theater-v3-vertical/frames")

BASE = """Recreate the attached image EXACTLY - same composition, same mascot pose, same
contraption, same line weights, same colors, same placement of every element.
Change ONLY the text defect named below; every other text string must stay
byte-identical. Never print any word twice in a row. Render each listed word
exactly once. Output one vertical 9:16 image on the same white paper."""

FIXES = {
    "slide_003": """Defect: the red warning box duplicates words at line wraps. It currently reads
"Calculate minimum and maximum maximum using FORMATION WINDOW ONLY. Using future
future observations introduces look-ahead bias."
Correct text, verbatim, each word exactly once:
"Calculate minimum and maximum using FORMATION WINDOW ONLY. Using future
observations introduces look-ahead bias."
Fix nothing else in the image.""",

    "slide_005": """Defect: the "Zero-Line" label is missing. Add a small handwritten "Zero-Line"
label with a short leader line pointing at the horizontal zero reference line of
the spread chart (the baseline of the flattened spread line), in the same ink
style as the other labels. Do not move or rewrite any existing text.""",

    "slide_008": """Defect: the X-axis label repeats the Y-axis parenthetical. It currently reads
"X-Axis: Relationship Stability (Spread Variability / Std Dev)".
Correct text, verbatim:
"X-Axis: Relationship Stability (Historical Distance)"
Keep the Y-axis label unchanged: "Y-Axis: Opportunity (Spread Variability / Std
Dev)". Fix nothing else in the image.""",

    "slide_009": """Defect: the title contains a spelling typo. It currently reads
"Distance vs. Pearson Apprach Diagnostic".
Correct title, verbatim:
"Distance vs. Pearson Approach Diagnostic"
Fix nothing else in the image; the table content is correct.""",

    "slide_010": """Defect: the bottom caption duplicates a word at a line wrap. It currently reads
"Optimization follows profiling: data loading and synchronization synchronization
matter as much as arithmetic speed."
Correct text, verbatim, each word exactly once:
"Optimization follows profiling: data loading and synchronization matter as
much as arithmetic speed."
Fix nothing else in the image.""",

    "slide_011": """Defect: the fifth red barrier gate has the wrong label. It currently reads
"Successful Backtest" and the image also contains an extra invented label
"TRADING WINDOW".
Corrections:
1. Fifth red gate label must read exactly: "Risk Limits" (keep the red barrier
   gate style of the other four).
2. Remove the stray "TRADING WINDOW" text; that area becomes quiet paper.
Fix nothing else in the image.""",
}


async def fix_one(page, name: str, body: str) -> bool:
    src = OUT / f"{name}.png"
    refs = [src]
    prompt = f"{BASE}\n\n## Fix\n\n{body}"

    log(f"[{name}] attaching current frame")
    if not await attach_files(page, refs):
        log(f"[{name}] attach failed")
        await shot(page, f"vfix_fail_{name}_attach")
        return False
    chips = 0
    for _ in range(40):
        chips = await page.locator("[aria-label*='Remove' i]").count()
        if chips >= len(refs):
            break
        await page.wait_for_timeout(500)
    log(f"[{name}] attachment chips: {chips}/{len(refs)}")
    await page.wait_for_timeout(1500)
    await page.wait_for_timeout(2500)
    try:
        baseline = {im["src"] for im in await page.evaluate(JS_COLLECT_IMAGES)}
    except Exception:
        baseline = set()
    if not await fill_prompt(page, prompt) or not await send_message(page):
        log(f"[{name}] send failed")
        await shot(page, f"vfix_fail_{name}_send")
        return False
    images = await wait_for_images(page, baseline, timeout_s=300, quiet_s=10)
    await shot(page, f"vfix_ok_{name}")
    images = [im for im in images if im["h"] > im["w"]]
    if not images:
        log(f"[{name}] no portrait image returned")
        return False
    data = await _grab_one(page, images[0])
    if data is None:
        log(f"[{name}] grab failed")
        return False
    (OUT / f"{name}.png").write_bytes(data)
    log(f"[{name}] fixed and saved ({len(data):,} bytes)")
    return True


async def run() -> int:
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
            await shot(page, "vfix_auth_fail")
            return 3
        editor, _sel = await wait_for_editor(page)
        if editor is None:
            log("ERROR: prompt editor not found")
            return 4
        for name, body in FIXES.items():
            ok = False
            for attempt in range(3):
                if await fix_one(page, name, body):
                    ok = True
                    break
                log(f"[{name}] fix attempt {attempt+1} failed")
                await asyncio.sleep(8)
            if not ok:
                failed.append(name)
                break  # stop on first failure: inspect visually before more spend
    if failed:
        log(f"failed: {failed}")
        return 1
    log("ALL FIXES DONE")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
