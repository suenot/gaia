#!/usr/bin/env python3
"""
Generate images on https://gemini.google.com/app using an *authorized* Firefox
session, driven through Camoufox (an anti-detect Firefox build) + Playwright.

Auth: Google cookies are read from the chosen Firefox profile and injected into
Camoufox (the Gemini session is cookie-based). See README.md and gemini_common.py.

Usage
-----
    python3 gemini_image_gen.py --prompt-file prompts/example.txt
    python3 gemini_image_gen.py --profile "Профиль 3" --prompt "a red fox in snow, 16:9" --keep-open

Outputs
-------
  * Generated images  -> ./output/<timestamp>_<n>.png
  * Debug screenshots -> ./debug/<step>.png   (use --debug)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import time
from pathlib import Path
from typing import List, Optional

from gemini_common import (
    OUTPUT_DIR,
    load_cookies,
    log,
    shot,
    is_logged_in,
    make_camoufox,
    open_app,
    wait_for_editor,
    fill_prompt,
    send_message,
    hold,
)

# JS: collect candidate generated-image sources currently in the DOM.
# Filters to reasonably large images served from Google's image CDN / blobs,
# excluding small UI icons and avatars.
JS_COLLECT_IMAGES = r"""
() => {
  const imgs = Array.from(document.querySelectorAll('img'));
  const out = [];
  for (const im of imgs) {
    const w = im.naturalWidth, h = im.naturalHeight;
    const src = im.currentSrc || im.src || '';
    if (!src) continue;
    if (w < 256 || h < 256) continue;
    const ok = src.includes('googleusercontent.com')
            || src.startsWith('blob:')
            || src.startsWith('data:image');
    if (!ok) continue;
    out.push({ src, w, h });
  }
  return out;
}
"""

# JS: extract a displayed <img> as a PNG data URL via canvas. Gemini serves
# generated images as blob: URLs that it revokes right after the <img> loads, so
# fetch(blobURL) fails — but the decoded bitmap is still in the <img>, and a
# same-origin blob image does not taint the canvas. Returns full *native* res.
JS_IMG_TO_DATAURL = r"""
async (src) => {
  const imgs = Array.from(document.querySelectorAll('img'));
  const im = imgs.find(i => (i.currentSrc || i.src) === src);
  if (!im) throw new Error('img element not found for src');
  try { if (im.decode) await im.decode(); } catch (e) {}
  const w = im.naturalWidth, h = im.naturalHeight;
  if (!w || !h) throw new Error('image has no natural size');
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  c.getContext('2d').drawImage(im, 0, 0, w, h);
  try {
    return { dataUrl: c.toDataURL('image/png'), w, h };
  } catch (e) {
    throw new Error('tainted:' + e.message);
  }
}
"""


async def wait_for_images(page, baseline: set, timeout_s: int, quiet_s: float) -> List[dict]:
    """Poll until new generated images appear and stop changing for `quiet_s`."""
    deadline = time.time() + timeout_s
    last_change = None
    seen: dict[str, dict] = {}
    while time.time() < deadline:
        try:
            imgs = await page.evaluate(JS_COLLECT_IMAGES)
        except Exception:  # noqa: BLE001
            imgs = []
        changed = False
        for im in imgs:
            if im["src"] in baseline or im["src"] in seen:
                continue
            seen[im["src"]] = im
            changed = True
            log(f"  new image: {im['w']}x{im['h']} {im['src'][:70]}...")
        if changed:
            last_change = time.time()
        if seen and last_change is not None and (time.time() - last_change) >= quiet_s:
            break
        await page.wait_for_timeout(1500)
    return list(seen.values())


async def download_images(page, images: List[dict]) -> List[Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved: List[Path] = []
    for idx, im in enumerate(images, 1):
        out = OUTPUT_DIR / f"{stamp}_{idx}.png"
        data = await _grab_one(page, im)
        if data is None:
            log(f"  download #{idx} failed (all methods)")
            continue
        out.write_bytes(data)
        log(f"  saved {out.name} ({len(data):,} bytes, {im['w']}x{im['h']})")
        saved.append(out)
    return saved


async def _grab_one(page, im: dict) -> Optional[bytes]:
    """Get PNG bytes for one image: canvas (native res) -> element screenshot."""
    try:
        res = await page.evaluate(JS_IMG_TO_DATAURL, im["src"])
        return base64.b64decode(res["dataUrl"].split(",", 1)[1])
    except Exception as e:  # noqa: BLE001
        log(f"  canvas grab failed ({str(e).splitlines()[0][:60]}); trying screenshot")
    try:
        count = await page.locator("img").count()
        for i in range(count):
            cand = page.locator("img").nth(i)
            src = await cand.evaluate("e => e.currentSrc || e.src")
            if src == im["src"]:
                return await cand.screenshot(type="png")
    except Exception as e:  # noqa: BLE001
        log(f"  screenshot grab failed: {str(e).splitlines()[0][:60]}")
    return None


async def run(args) -> int:
    profile_path, cookies = load_cookies(args.profile)
    log(f"Firefox profile: {profile_path.name}")
    log(f"Loaded {len(cookies)} google cookies")

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        log("ERROR: no prompt provided (use --prompt or --prompt-file)")
        return 2
    log(f"Prompt ({len(prompt)} chars): {prompt[:80].replace(chr(10), ' ')}...")

    async with make_camoufox(args.headless) as context:
        page = await open_app(context, cookies, debug=args.debug)

        if not await is_logged_in(page):
            log("ERROR: not logged in (redirected to sign-in or editor missing).")
            log(f"  current url: {page.url}")
            await shot(page, "02_not_logged_in")
            if args.keep_open:
                await hold(page)
            return 3
        log("Logged in ✓")

        editor, sel = await wait_for_editor(page)
        if editor is None:
            log("ERROR: prompt editor not found.")
            await shot(page, "02_no_editor")
            if args.keep_open:
                await hold(page)
            return 4
        log(f"Editor: {sel}")

        try:
            baseline_imgs = await page.evaluate(JS_COLLECT_IMAGES)
        except Exception:  # noqa: BLE001
            baseline_imgs = []
        baseline = {im["src"] for im in baseline_imgs}

        await shot(page, "03_before_type", args.debug)
        if not await fill_prompt(page, prompt) or not await send_message(page):
            log("ERROR: could not enter/send the prompt.")
            await shot(page, "03b_type_failed")
            if args.keep_open:
                await hold(page)
            return 4
        log("Prompt sent; waiting for image generation...")
        await shot(page, "04_sent", args.debug)

        images = await wait_for_images(page, baseline, timeout_s=args.timeout, quiet_s=args.quiet)
        await shot(page, "05_result", args.debug)

        if not images:
            log("No images detected. Check debug/05_result.png — Gemini may have "
                "returned text only, hit a limit, or needs a different prompt.")
            if args.keep_open:
                await hold(page)
            return 5

        log(f"Detected {len(images)} image(s). Downloading...")
        saved = await download_images(page, images)
        log(f"Done. Saved {len(saved)} file(s) to {OUTPUT_DIR}")

        if args.keep_open:
            await hold(page)
        return 0 if saved else 5


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="default-release",
                   help="Firefox profile name/dir/path logged into Gemini (default: default-release)")
    p.add_argument("--prompt", default="", help="Inline prompt text")
    p.add_argument("--prompt-file", default="", help="Read prompt from this file")
    p.add_argument("--headless", action="store_true",
                   help="Run headless (default: visible window — more reliable with Google)")
    p.add_argument("--timeout", type=int, default=240, help="Max seconds to wait for images (default 240)")
    p.add_argument("--quiet", type=float, default=8.0,
                   help="Seconds of no new images before considering generation done (default 8)")
    p.add_argument("--keep-open", action="store_true", help="Keep the browser open after finishing")
    p.add_argument("--debug", action="store_true", help="Save step screenshots to ./debug")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
