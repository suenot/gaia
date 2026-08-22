#!/usr/bin/env python3
"""Sixth pass, slide_010 only: REGENERATE from scratch, no attachment.

The new-input-ui composer (A/B rollout mid-session) refuses to submit while
an attachment chip is present - uploads finish server-side (200 on
push.clients6.google.com) but the send path stays blocked, and the composer
has no send <button> to click. Text-only prompts still submit fine, so this
pass drops the attachment entirely and re-runs the ORIGINAL batch prompt for
slide_010, hardened with the pass-3 lesson: the caption's duplicated word
lives at a line-wrap boundary, so the caption is pinned with explicit lines.
"""
import asyncio
from pathlib import Path

from gemini_common import (
    load_cookies, log, shot, is_logged_in, make_camoufox,
    open_app, wait_for_editor, fill_prompt, send_message,
)
from gemini_image_gen import JS_COLLECT_IMAGES, wait_for_images, _grab_one

OUT = Path("/Users/suenot/projects/trading/marketmaker/video_maker/temp/distance-approach-pairs-trading_ink-theater-v3-vertical/frames")

STYLE = """Vertical 9:16 hand-drawn ink illustration on white paper, clean confident
linework, minimal water-color accents, tiny inventor mascot with brass
goggles. Style of a19th-century patent drawing. Plenty of quiet paper around
the edges - keep ALL text and drawing inside the middle band of the sheet
(horizontal 8%-78%, vertical 9%-75%) so a vertical video crop never clips it.
No watermark, no signature."""

PROMPT = f"""{STYLE}

Slide: "Rust implementation". Mascot operates a tall steam-driven sorting
engine built vertically: a SIMD gear block turning a bounded-heap hopper,
with async belts feeding it.
Labels: "TradingSignal Enum: Long, Short, Neutral states", "Bounded Heap",
"SIMD Hot Loop", "Asynchronous Tasks", "Numerical Slices & Explicit
Ownership".
Captions: "Joining async results and sorting combined candidates.",
"Processes multiple floating-point differences in one CPU operation for
Euclidean distances.", "Processes independent ranges of the universe
concurrently."
Bottom caption, rendered as EXACTLY these two lines - copy each line
character by character, never repeat the last word of a line at the start of
the next line:
Line 1: "Optimization follows profiling: data loading and"
Line 2: "synchronization matter as much as arithmetic speed."
The word "synchronization" appears only on Line 2.

Output one vertical 9:16 image."""


async def fix_one(page, name: str, old_bytes: bytes) -> bool:
    try:
        baseline = {im["src"] for im in await page.evaluate(JS_COLLECT_IMAGES)}
    except Exception:
        baseline = set()
    if not await fill_prompt(page, PROMPT) or not await send_message(page):
        log(f"[{name}] send failed")
        await shot(page, f"vfix6_fail_{name}_send")
        return False
    images = await wait_for_images(page, baseline, timeout_s=300, quiet_s=10)
    await shot(page, f"vfix6_ok_{name}")
    images = [im for im in images if im["h"] > im["w"]]
    if not images:
        log(f"[{name}] no portrait image returned")
        return False
    data = await _grab_one(page, images[0])
    if data is None:
        log(f"[{name}] grab failed")
        return False
    if data == old_bytes:
        log(f"[{name}] grabbed stale image, not a new render")
        return False
    (OUT / f"{name}.png").write_bytes(data)
    log(f"[{name}] regenerated and saved ({len(data):,} bytes)")
    return True


async def fresh_chat(page) -> None:
    await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)


async def run() -> int:
    try:
        _, cookies = load_cookies("default-release")
    except Exception as e:
        log(f"cookie extraction failed ({str(e).splitlines()[0][:60]}); relying on the persistent gaia profile")
        cookies = []
    src = OUT / "slide_010.png"
    old = src.read_bytes()
    async with make_camoufox(False) as context:
        page = await open_app(context, cookies)
        if not await is_logged_in(page):
            log("ERROR: not logged into Gemini")
            await shot(page, "vfix6_auth_fail")
            return 3
        for attempt in range(3):
            if attempt:
                await fresh_chat(page)
            editor, _sel = await wait_for_editor(page)
            if editor is None:
                log("ERROR: prompt editor not found")
                return 4
            if await fix_one(page, "slide_010", old):
                log("ALL SIXTH-PASS FIXES DONE")
                return 0
            log(f"[slide_010] fix attempt {attempt+1} failed")
            await asyncio.sleep(10)
    log("failed: ['slide_010']")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
