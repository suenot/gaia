#!/usr/bin/env python3
"""Third pass on the duplicated-word frames: force explicit line wrapping.

Nano Banana duplicates a word at ITS chosen line-wrap boundary and even a
word-count callout does not break the habit (slide_003 kept "maximum maximum"
through pass 2). Forcing different, explicit line breaks moves the duplicated
word away from the boundary it keeps tripping over.
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
The ONLY change: re-set one text block. The attached image prints a word twice
in a row at a line break; that is wrong. Render the corrected text below with
EXACTLY the line breaks shown - copy each line character by character, and do
not repeat the last word of a line at the start of the next line. Fix nothing
else in the image."""

FIXES = {
    "slide_003": """Block to re-set: the red warning box. The attached image wrongly reads
"maximum" twice in a row.
Re-set it to EXACTLY these four lines, nothing added, nothing repeated:
Line 1: "Calculate minimum and"
Line 2: "maximum using FORMATION"
Line 3: "WINDOW ONLY. Using future"
Line 4: "observations introduces look-ahead bias."
The word "maximum" appears only on Line 2. The word "future" appears only on
Line 3. Everything else in the image stays identical.""",

    "slide_010": """Block to re-set: the bottom caption. The attached image wrongly reads
"synchronization" twice in a row.
Re-set it to EXACTLY these two lines, nothing added, nothing repeated:
Line 1: "Optimization follows profiling: data loading and"
Line 2: "synchronization matter as much as arithmetic speed."
The word "synchronization" appears only on Line 2. Everything else in the
image stays identical.""",
}


async def fix_one(page, name: str, body: str) -> bool:
    src = OUT / f"{name}.png"
    prompt = f"{BASE}\n\n## Fix\n\n{body}"

    log(f"[{name}] attaching current frame")
    if not await attach_files(page, [src]):
        log(f"[{name}] attach failed")
        await shot(page, f"vfix3_fail_{name}_attach")
        return False
    chips = 0
    for _ in range(40):
        chips = await page.locator("[aria-label*='Remove' i]").count()
        if chips >= 1:
            break
        await page.wait_for_timeout(500)
    log(f"[{name}] attachment chips: {chips}/1")
    await page.wait_for_timeout(1500)
    await page.wait_for_timeout(2500)
    try:
        baseline = {im["src"] for im in await page.evaluate(JS_COLLECT_IMAGES)}
    except Exception:
        baseline = set()
    if not await fill_prompt(page, prompt) or not await send_message(page):
        log(f"[{name}] send failed")
        await shot(page, f"vfix3_fail_{name}_send")
        return False
    images = await wait_for_images(page, baseline, timeout_s=300, quiet_s=10)
    await shot(page, f"vfix3_ok_{name}")
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
            await shot(page, "vfix3_auth_fail")
            return 3
        editor, _sel = await wait_for_editor(page)
        if editor is None:
            log("ERROR: prompt editor not found")
            return 4
        for name, body in FIXES.items():
            if name == "slide_010" and input_skip_010():
                continue
            ok = False
            for attempt in range(3):
                if await fix_one(page, name, body):
                    ok = True
                    break
                log(f"[{name}] fix attempt {attempt+1} failed")
                await asyncio.sleep(8)
            if not ok:
                failed.append(name)
                break
    if failed:
        log(f"failed: {failed}")
        return 1
    log("ALL THIRD-PASS FIXES DONE")


def input_skip_010() -> bool:
    """Skip slide_010 if the second pass already fixed it (file newer than pass 2 start)."""
    import sys
    return "--skip-010" in sys.argv


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
