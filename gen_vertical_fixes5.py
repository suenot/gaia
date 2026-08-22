#!/usr/bin/env python3
"""Fifth pass, slide_010 only: REPHRASED prompt, fresh chat per attempt.

Pass 3/4 showed the exact recreate-style prompt for slide_010 renders no
response at all - even in a brand-new chat. Pass 1 proved Gemini CAN edit this
frame, so this pass changes the wording: a short, direct "fix the typo" edit
request instead of the recreate-exactly formula, with the corrected caption
given as explicit lines.
"""
import asyncio
from pathlib import Path

from gemini_common import (
    load_cookies, log, shot, is_logged_in, make_camoufox,
    open_app, wait_for_editor, fill_prompt, send_message, attach_files,
    _composer_has_text,
)
from gemini_image_gen import JS_COLLECT_IMAGES, wait_for_images, _grab_one

OUT = Path("/Users/suenot/projects/trading/marketmaker/video_maker/temp/distance-approach-pairs-trading_ink-theater-v3-vertical/frames")

PROMPT = """The attached image has one typo: in the bottom caption the word
"synchronization" is written twice in a row. Fix it.

Edit the image so the bottom caption reads exactly these two lines:
Optimization follows profiling: data loading and
synchronization matter as much as arithmetic speed.

Keep everything else exactly as it is - same drawing, same layout, same colors.
Reply with the corrected image."""


async def fix_one(page, name: str) -> bool:
    src = OUT / f"{name}.png"
    # Attach a freshly re-encoded copy: the original PNG's uploads started
    # failing server-side ("Something went wrong while uploading the file"),
    # which wedges the composer - Enter stops submitting.
    from PIL import Image
    ask = Path(f"/tmp/{name}_ask.png")
    with Image.open(src) as im:
        im.convert("RGB").save(ask, "PNG", optimize=True)
    log(f"[{name}] re-encoded ask copy: {ask.stat().st_size:,} bytes")

    log(f"[{name}] attaching current frame")
    if not await attach_files(page, [ask]):
        log(f"[{name}] attach failed")
        await shot(page, f"vfix5_fail_{name}_attach")
        return False
    chips = 0
    for _ in range(40):
        chips = await page.locator("[aria-label*='Remove' i]").count()
        if chips >= 1:
            break
        err = await page.locator("text=/something went wrong/i").count()
        if err:
            log(f"[{name}] upload error toast - attach rejected server-side")
            return False
        await page.wait_for_timeout(500)
    log(f"[{name}] attachment chips: {chips}/1")
    await page.wait_for_timeout(1500)
    await page.wait_for_timeout(2500)
    try:
        baseline = {im["src"] for im in await page.evaluate(JS_COLLECT_IMAGES)}
    except Exception:
        baseline = set()
    if not await fill_prompt(page, PROMPT):
        log(f"[{name}] fill failed")
        await shot(page, f"vfix5_fail_{name}_send")
        return False
    sent = await send_message(page)
    if not sent:
        # Enter stopped submitting in the current composer build; click the
        # send button directly as a fallback.
        log(f"[{name}] Enter send failed; clicking send button")
        try:
            btn = page.locator("button[aria-label*='Send' i]").first
            await btn.click(timeout=5000)
            await page.wait_for_timeout(2000)
            sent = not await _composer_has_text(page)
        except Exception as e:  # noqa: BLE001
            log(f"[{name}] send-button click failed: {str(e).splitlines()[0][:60]}")
    if not sent:
        log(f"[{name}] send failed")
        await shot(page, f"vfix5_fail_{name}_send")
        return False
    images = await wait_for_images(page, baseline, timeout_s=300, quiet_s=10)
    await shot(page, f"vfix5_ok_{name}")
    images = [im for im in images if im["h"] > im["w"]]
    if not images:
        log(f"[{name}] no portrait image returned")
        return False
    data = await _grab_one(page, images[0])
    if data is None:
        log(f"[{name}] grab failed")
        return False
    if data == src.read_bytes():
        log(f"[{name}] grabbed attachment echo, not a new render")
        return False
    (OUT / f"{name}.png").write_bytes(data)
    log(f"[{name}] fixed and saved ({len(data):,} bytes)")
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
    async with make_camoufox(False) as context:
        page = await open_app(context, cookies)
        if not await is_logged_in(page):
            log("ERROR: not logged into Gemini")
            await shot(page, "vfix5_auth_fail")
            return 3
        for attempt in range(3):
            if attempt:
                await fresh_chat(page)
            editor, _sel = await wait_for_editor(page)
            if editor is None:
                log("ERROR: prompt editor not found")
                return 4
            if await fix_one(page, "slide_010"):
                log("ALL FIFTH-PASS FIXES DONE")
                return 0
            log(f"[slide_010] fix attempt {attempt+1} failed")
            await asyncio.sleep(10)
    log("failed: ['slide_010']")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
