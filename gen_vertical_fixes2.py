#!/usr/bin/env python3
"""Second, harsher pass on the two frames whose duplicated-word defect
survived the first edit (slide_003, slide_010).

The first edit asked to "fix the text" and Gemini reproduced the duplication.
This pass names the duplicated words explicitly and demands each appear ONCE,
with the corrected text quoted word by word.
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
The ONLY change: the named text block currently DUPLICATES words at line wraps;
in your output every word of that block appears EXACTLY ONCE. Read each line of
the corrected text below word by word and render precisely those words in that
order - do not copy the attached image's text, it is wrong. Fix nothing else."""

FIXES = {
    "slide_003": """Defective block (red warning box). The attached image wrongly prints the words
"maximum" and "future" twice in a row.
Correct block, verbatim, every word exactly once:
"Calculate minimum and maximum using FORMATION WINDOW ONLY. Using future
observations introduces look-ahead bias."
Word count check: minimum=1, and=1, maximum=1, using=2 (two separate clauses),
FORMATION=1, WINDOW=1, ONLY=1, future=1, observations=1, introduces=1,
look-ahead=1, bias=1. The words "maximum" and "future" each appear ONE time
total. Everything else in the image stays identical.""",

    "slide_010": """Defective block (bottom caption). The attached image wrongly prints the word
"synchronization" twice in a row.
Correct block, verbatim, every word exactly once:
"Optimization follows profiling: data loading and synchronization matter as
much as arithmetic speed."
Word count check: synchronization appears ONE time total, immediately followed
by "matter". Everything else in the image stays identical.""",
}


async def fix_one(page, name: str, body: str) -> bool:
    src = OUT / f"{name}.png"
    prompt = f"{BASE}\n\n## Fix\n\n{body}"

    log(f"[{name}] attaching current frame")
    if not await attach_files(page, [src]):
        log(f"[{name}] attach failed")
        await shot(page, f"vfix2_fail_{name}_attach")
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
        await shot(page, f"vfix2_fail_{name}_send")
        return False
    images = await wait_for_images(page, baseline, timeout_s=300, quiet_s=10)
    await shot(page, f"vfix2_ok_{name}")
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
            await shot(page, "vfix2_auth_fail")
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
                break
    if failed:
        log(f"failed: {failed}")
        return 1
    log("ALL SECOND-PASS FIXES DONE")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
