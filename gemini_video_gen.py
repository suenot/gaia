#!/usr/bin/env python3
"""
Generate a VIDEO from an image on https://gemini.google.com/app (Veo), using an
*authorized* Firefox session via Camoufox + Playwright.

Gemini only does image->video in a *fresh* chat where the first message contains
BOTH the source image AND a text prompt. This script:
  1. opens a brand-new chat (fresh context => fresh /app conversation),
  2. (optionally) selects the "Video" tool,
  3. attaches the source image,
  4. types the motion prompt,
  5. sends, waits for the Veo video, and downloads the .mp4.

Auth: Google cookies from the Firefox profile are injected into Camoufox
(see gemini_common.py / README.md).

Usage
-----
    python3 gemini_video_gen.py --image output/some.png --prompt "slow cinematic zoom in, subtle parallax"
    python3 gemini_video_gen.py --image in.png --prompt-file prompts/motion.txt --debug

    # Inspect the UI (find upload input / tool buttons / result controls) without committing:
    python3 gemini_video_gen.py --image in.png --prompt "x" --explore --keep-open

Outputs
-------
  * Generated video   -> ./output/<timestamp>.mp4
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
    find_editor,
    hold,
)

# The composer's "+" button opens an "Upload & tools" menu containing both
# "Upload files" (file chooser) and the "Create video" tool toggle. (UI is in
# English on this account.)
UPLOAD_TOOLS_BTN = "button[aria-label='Upload & tools']"
# Words that identify a "Download" / overflow control on the generated video.
DOWNLOAD_WORDS = ["download", "save", "скачать", "сохранить"]
MORE_WORDS = ["more", "options", "ещё", "еще"]

# JS: list <video> elements with a usable source.
JS_COLLECT_VIDEOS = r"""
() => {
  const vids = Array.from(document.querySelectorAll('video'));
  const out = [];
  for (const v of vids) {
    const srcEl = v.querySelector('source');
    const src = v.currentSrc || v.src || (srcEl ? srcEl.src : '') || '';
    if (!src) continue;
    out.push({ src, w: v.videoWidth || 0, h: v.videoHeight || 0, dur: v.duration || 0 });
  }
  return out;
}
"""

# JS: dump UI elements useful for locating upload inputs / tool / result buttons.
JS_DUMP_UI = r"""
() => {
  const fileInputs = Array.from(document.querySelectorAll('input[type=file]')).map(i => ({
    accept: i.accept, multiple: i.multiple, hidden: i.offsetParent === null,
  }));
  const buttons = Array.from(document.querySelectorAll('button,[role=button]')).map(b => ({
    label: (b.getAttribute('aria-label') || '').trim(),
    text: (b.innerText || '').trim().slice(0, 40),
  })).filter(b => b.label || b.text);
  return { fileInputs, buttons: buttons.slice(0, 120) };
}
"""

# JS: fetch a resource in-page and return base64 (works for https; and for blob:
# URLs that are still alive). Throws on revoked/streamed blobs.
JS_FETCH_B64 = r"""
async (src) => {
  const r = await fetch(src);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const b = await r.blob();
  const buf = await b.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let bin = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return { b64: btoa(bin), type: b.type || '', size: bytes.length };
}
"""


async def dump_ui(page, tag: str) -> None:
    try:
        info = await page.evaluate(JS_DUMP_UI)
    except Exception as e:  # noqa: BLE001
        log(f"  dump_ui failed: {e}")
        return
    log(f"=== UI dump [{tag}] ===")
    log(f"  file inputs: {info['fileInputs']}")
    labeled = [b for b in info["buttons"] if b["label"] or b["text"]]
    for b in labeled:
        lab = b["label"] or "-"
        txt = b["text"] or "-"
        log(f"  btn  aria='{lab}'  text='{txt}'")


async def click_word_button(page, words: List[str], timeout_ms: int = 4000) -> bool:
    """Click the first button/menuitem whose aria-label or text matches any word."""
    sel = "button, [role=button], [role=menuitem], [role=option], a"
    try:
        await page.wait_for_selector(sel, timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    n = await page.locator(sel).count()
    for i in range(n):
        el = page.locator(sel).nth(i)
        try:
            if not await el.is_visible():
                continue
            label = (await el.get_attribute("aria-label") or "")
            text = (await el.inner_text() or "")
            hay = f"{label} {text}".lower()
            if any(w in hay for w in words):
                await el.click(timeout=3000)
                log(f"  clicked button matching {words}: aria='{label[:40]}' text='{text[:40]}'")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def dismiss_dialogs(page) -> None:
    """Dismiss feature-promo dialogs (e.g. 'Create videos with Gemini Omni') that
    overlay the composer and swallow the Enter key. Never clicks a primary CTA
    like 'Try it' — only close/dismiss controls, then Escape as a last resort."""
    for sel in ("button[aria-label='Close']", "button[aria-label*='close' i]",
                "button[aria-label='Dismiss']"):
        b = page.locator(sel)
        try:
            if await b.count() > 0 and await b.first.is_visible():
                await b.first.click(timeout=1500)
                log(f"  closed dialog via {sel}")
                await page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
    for txt in ("Not now", "No thanks", "Maybe later", "Dismiss", "Skip"):
        b = page.locator(f"button:has-text('{txt}')")
        try:
            if await b.count() > 0 and await b.first.is_visible():
                await b.first.click(timeout=1500)
                log(f"  dismissed dialog: {txt}")
                await page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
    try:
        if await page.locator("mat-dialog-container, [role=dialog]").count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            log("  pressed Escape to close a dialog")
    except Exception:  # noqa: BLE001
        pass


async def submit_and_verify(page, prompt: str) -> bool:
    """Submit the message and confirm it actually went out.

    With an attachment, the Send button stays disabled until the image finishes
    uploading, so we (a) wait for it to become enabled, (b) click it, and
    (c) verify the composer cleared — retrying with Enter if it didn't."""
    snippet = " ".join(prompt.split())[:25]
    btn = page.locator("button[aria-label='Send message']")

    # Wait up to 40s for the Send button to become enabled (upload finishing).
    for _ in range(40):
        try:
            if await btn.count() > 0 and await btn.first.is_enabled():
                break
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1000)

    for attempt in range(3):
        clicked = False
        try:
            if await btn.count() > 0 and await btn.first.is_enabled():
                await btn.first.click(timeout=4000)
                clicked = True
        except Exception as e:  # noqa: BLE001
            log(f"  send-button click failed: {str(e).splitlines()[0]}")
        if not clicked:
            ed, _sel = await find_editor(page)
            if ed is not None:
                try:
                    await ed.press("Enter")
                except Exception:  # noqa: BLE001
                    pass
        await page.wait_for_timeout(2500)

        # Verify: the composer should no longer contain the prompt text.
        ed, _sel = await find_editor(page)
        txt = ""
        if ed is not None:
            try:
                txt = (await ed.inner_text()) or ""
            except Exception:  # noqa: BLE001
                pass
        if not snippet or snippet not in txt:
            return True
        log(f"  send attempt {attempt + 1}: composer still has text, retrying")
        await page.wait_for_timeout(1000)
    return False


async def ensure_upload_button(page, attempts: int = 5) -> bool:
    """The composer can load in a collapsed 'Ask Gemini' splash state where the
    'Upload & tools' (+) button isn't present yet. Focusing/clicking the editor
    expands the full toolbar. Retry until the button is visible."""
    btn = page.locator(UPLOAD_TOOLS_BTN)
    for _ in range(attempts):
        try:
            if await btn.count() > 0 and await btn.first.is_visible():
                return True
        except Exception:  # noqa: BLE001
            pass
        ed, _sel = await find_editor(page)
        if ed is not None:
            try:
                await ed.click(timeout=4000)
            except Exception:  # noqa: BLE001
                try:
                    await ed.focus()
                except Exception:  # noqa: BLE001
                    pass
        await page.wait_for_timeout(1500)
    try:
        return await btn.count() > 0 and await btn.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def _open_upload_menu(page) -> bool:
    btn = page.locator(UPLOAD_TOOLS_BTN)
    if await btn.count() == 0:
        log(f"  '{UPLOAD_TOOLS_BTN}' not found")
        return False
    await btn.first.click()
    try:
        await page.wait_for_selector("[role=menu]", state="visible", timeout=8000)
    except Exception:  # noqa: BLE001
        return False
    await page.wait_for_timeout(400)
    return True


async def enable_create_video(page, debug: bool) -> bool:
    """Toggle on the 'Create video' tool in the Upload & tools menu."""
    if not await _open_upload_menu(page):
        return False
    item = page.locator("[role=menuitemcheckbox]:has-text('Create video')")
    if await item.count() == 0:
        item = page.locator("button:has-text('Create video')")
    if await item.count() == 0:
        log("  'Create video' tool not found in menu")
        await page.keyboard.press("Escape")
        return False
    await item.first.click()
    await page.wait_for_timeout(800)
    await page.keyboard.press("Escape")  # close the menu if it stays open
    await page.wait_for_timeout(400)
    log("  'Create video' tool enabled")
    await shot(page, "10_video_tool", debug)
    return True


async def attach_image(page, image_path: Path, debug: bool) -> bool:
    """Attach the source image via Upload & tools -> Upload files (file chooser).

    Gemini has no persistent <input type=file>; the menu item opens the native
    file chooser, which Playwright intercepts with expect_file_chooser().
    """
    if not await _open_upload_menu(page):
        return False
    up = page.locator("button[aria-label*='Upload files' i]")
    if await up.count() == 0:
        up = page.locator("[role=menuitem]:has-text('Upload files')")
    if await up.count() == 0:
        log("  'Upload files' item not found")
        await page.keyboard.press("Escape")
        return False
    try:
        async with page.expect_file_chooser(timeout=10_000) as fc_info:
            await up.first.click()
        chooser = await fc_info.value
        await chooser.set_files(str(image_path))
        log("  image sent to file chooser")
    except Exception as e:  # noqa: BLE001
        log(f"  file chooser failed: {str(e).splitlines()[0]}")
        return False
    ok = await _wait_attachment(page)
    await shot(page, "12_attached", debug)
    return ok


async def _wait_attachment(page, timeout_s: int = 40) -> bool:
    """Wait for the uploaded image preview/thumbnail to finish appearing."""
    deadline = time.time() + timeout_s
    sel = (
        "img[src^='blob:'], img[src^='data:image'], "
        "[data-test-id*='file' i], [class*='attachment' i], [class*='thumbnail' i], "
        "[class*='uploaded' i], [aria-label*='Remove' i]"
    )
    while time.time() < deadline:
        try:
            if await page.locator(sel).count() > 0:
                await page.wait_for_timeout(2000)  # let the upload finish processing
                return True
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1000)
    log("  (no attachment preview detected within timeout)")
    return False


async def wait_for_video(page, timeout_s: int, quiet_s: float, debug: bool) -> List[dict]:
    """Poll until a generated <video> appears and its metadata stabilizes."""
    deadline = time.time() + timeout_s
    last_change = None
    seen: dict[str, dict] = {}
    last_log = 0.0
    while time.time() < deadline:
        try:
            vids = await page.evaluate(JS_COLLECT_VIDEOS)
        except Exception:  # noqa: BLE001
            vids = []
        changed = False
        for v in vids:
            key = v["src"]
            if key not in seen:
                seen[key] = v
                changed = True
                log(f"  new video: {v['w']}x{v['h']} dur={v['dur']:.1f}s {key[:60]}...")
        if changed:
            last_change = time.time()
            await shot(page, "20_video_appeared", debug)
        if seen and last_change is not None and (time.time() - last_change) >= quiet_s:
            break
        if time.time() - last_log > 20:
            elapsed = int(time.time() - (deadline - timeout_s))
            log(f"  ...still waiting for video ({elapsed}s elapsed)")
            last_log = time.time()
            if debug and elapsed and elapsed % 60 < 2:
                await shot(page, f"wait_{elapsed}s", True)
        await page.wait_for_timeout(2000)
    return list(seen.values())


async def download_video(page, video: dict, debug: bool) -> Optional[Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    src = video["src"]

    # Method 1: APIRequestContext — carries the context's google.com cookies and
    # is not subject to page CORS. Gemini's video src is a *.usercontent.google.com
    # download URL, which this fetches directly.
    if src.startswith("http"):
        try:
            resp = await page.context.request.get(src, timeout=120_000)
            if resp.ok:
                body = await resp.body()
                if len(body) > 10_000:
                    out.write_bytes(body)
                    log(f"  saved {out.name} via context.request ({len(body):,} bytes)")
                    return out
                log(f"  context.request returned only {len(body)} bytes")
            else:
                log(f"  context.request HTTP {resp.status}")
        except Exception as e:  # noqa: BLE001
            log(f"  context.request failed: {str(e).splitlines()[0][:70]}")

    # Method 2: in-page fetch (works for live blob: URLs).
    try:
        res = await page.evaluate(JS_FETCH_B64, src)
        if res.get("size", 0) > 10_000:
            out.write_bytes(base64.b64decode(res["b64"]))
            log(f"  saved {out.name} via in-page fetch ({res['size']:,} bytes)")
            return out
    except Exception as e:  # noqa: BLE001
        log(f"  in-page fetch failed: {str(e).splitlines()[0][:70]}")

    # Method 3: UI 'Download video' button. It may open a submenu (resolution
    # options) before the actual download fires, so click again inside the menu.
    try:
        await _hover_video(page)
        async with page.expect_download(timeout=30_000) as dl_info:
            clicked = await click_word_button(page, DOWNLOAD_WORDS)
            if clicked:
                await page.wait_for_timeout(900)
                # If a resolution/format submenu appeared, pick the first option.
                await click_word_button(page, DOWNLOAD_WORDS + ["1080", "720", "original", "mp4"])
            else:
                await click_word_button(page, MORE_WORDS)
                await page.wait_for_timeout(800)
                clicked = await click_word_button(page, DOWNLOAD_WORDS)
            if not clicked:
                raise RuntimeError("no download control found")
        dl = await dl_info.value
        await dl.save_as(str(out))
        log(f"  saved {out.name} via UI download button")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"  UI download failed: {str(e).splitlines()[0][:70]}")
        await shot(page, "21_download_failed", True)
    return None


async def _hover_video(page) -> None:
    try:
        vid = page.locator("video").first
        if await vid.count() > 0:
            await vid.hover(timeout=3000)
            await page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass


async def run(args) -> int:
    image_path = None
    if args.image:
        image_path = Path(args.image).expanduser()
        if not image_path.is_file():
            log(f"ERROR: image not found: {image_path}")
            return 2

    profile_path, cookies = load_cookies(args.profile)
    log(f"Firefox profile: {profile_path.name}; {len(cookies)} cookies; "
        f"image: {image_path.name if image_path else '(none — text-to-video)'}")

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
            log("ERROR: not logged in. current url: " + page.url)
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

        # The minimal "Ask Gemini" splash bar appears before the full composer;
        # focusing the editor expands the toolbar so the "+" (Upload & tools) shows.
        if not await ensure_upload_button(page):
            log("  warning: 'Upload & tools' button did not appear")
        await page.wait_for_timeout(500)

        if args.explore:
            await dump_ui(page, "fresh chat")

        await dismiss_dialogs(page)

        # Enable the "Create video" tool (Veo). On by default; --no-video-tool skips.
        if args.video_tool:
            log("Enabling 'Create video' tool...")
            if not await enable_create_video(page, args.debug):
                log("  (could not toggle Create video; continuing — Gemini may still "
                    "infer video from the image + prompt)")
            # Toggling the tool can trigger a 'Create videos' onboarding dialog.
            await dismiss_dialogs(page)

        # Attach the source image (image->video). Skipped for text->video.
        if image_path is not None:
            log("Attaching source image...")
            if not await attach_image(page, image_path, args.debug):
                log("ERROR: could not attach the image.")
                if args.explore:
                    await dump_ui(page, "attach failed")
                await shot(page, "11_attach_failed", True)
                if args.keep_open:
                    await hold(page)
                return 6
            log("Image attached ✓")
            await dismiss_dialogs(page)
        else:
            log("Text-to-video: no image; prompt should explicitly ask for a video.")

        # Type the motion prompt.
        if not await fill_prompt(page, prompt):
            log("ERROR: could not enter the prompt.")
            await shot(page, "13_fill_failed", True)
            if args.keep_open:
                await hold(page)
            return 4
        await dismiss_dialogs(page)
        await shot(page, "13_ready_to_send", args.debug)

        if args.explore and args.no_send:
            log("--explore --no-send: stopping before send so you can inspect.")
            await dump_ui(page, "ready to send")
            if args.keep_open:
                await hold(page)
            return 0

        if not await submit_and_verify(page, prompt):
            log("ERROR: could not send the message (composer still holds the text).")
            await shot(page, "14b_send_failed", True)
            if args.keep_open:
                await hold(page)
            return 4
        log("Sent; waiting for video generation (this can take a few minutes)...")
        await shot(page, "14_sent", args.debug)

        videos = await wait_for_video(page, timeout_s=args.timeout, quiet_s=args.quiet, debug=args.debug)
        await shot(page, "20_result", args.debug)

        if not videos:
            log("No video detected. Check debug/20_result.png — the account may lack "
                "Veo access, the request hit a limit, or the UI changed.")
            if args.explore:
                await dump_ui(page, "no video")
            if args.keep_open:
                await hold(page)
            return 5

        # Prefer the longest/most-defined video if several appeared.
        videos.sort(key=lambda v: (v.get("dur") or 0, v.get("w") or 0), reverse=True)
        log(f"Detected {len(videos)} video(s). Downloading the best one...")
        saved = await download_video(page, videos[0], args.debug)
        if saved:
            log(f"Done. Saved {saved}")
        else:
            log("Could not download the video automatically. Use --keep-open and grab "
                "it manually, or check debug/21_download_failed.png.")

        if args.keep_open:
            await hold(page)
        return 0 if saved else 5


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", default="",
                   help="Path to a source image to animate (image->video). Omit for "
                        "text->video (prompt-only).")
    p.add_argument("--prompt", default="", help="Inline motion/animation prompt")
    p.add_argument("--prompt-file", default="", help="Read the prompt from this file")
    p.add_argument("--profile", default="default-release",
                   help="Firefox profile name/dir/path logged into Gemini (default: default-release)")
    p.add_argument("--video-tool", action="store_true", default=False,
                   help="Open the dedicated 'Create video' studio instead of the plain "
                        "chat flow (experimental; default off — the plain image+prompt "
                        "chat flow is what generates inline video)")
    p.add_argument("--headless", action="store_true", help="Run headless (visible is more reliable)")
    p.add_argument("--timeout", type=int, default=600, help="Max seconds to wait for the video (default 600)")
    p.add_argument("--quiet", type=float, default=10.0,
                   help="Seconds of no change before generation is considered done (default 10)")
    p.add_argument("--keep-open", action="store_true", help="Keep the browser open after finishing")
    p.add_argument("--debug", action="store_true", help="Save step screenshots to ./debug")
    p.add_argument("--explore", action="store_true", help="Dump UI (file inputs, button labels) to help adapt selectors")
    p.add_argument("--no-send", action="store_true", help="With --explore: stop before sending (inspect the composer)")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
