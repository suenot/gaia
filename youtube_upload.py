#!/usr/bin/env python3
"""Upload a video to YouTube through the logged-in Camoufox session — no Data API,
no OAuth, no API keys. It drives **YouTube Studio** (studio.youtube.com) the same
way the other GAIA scripts drive Gemini / NotebookLM: reusing the persistent
Google session (see gemini_common: make_camoufox / prepare_persistent_page).

What it does
------------
  1. Open YouTube Studio, click Create -> "Upload videos".
  2. Pick the video file (hidden <input type=file>).
  3. Fill Details: title, description, tags (behind "Show more"), thumbnail,
     and the required "Not made for kids" audience answer.
  4. Click through Details -> Video elements -> Checks -> Visibility.
  5. Set visibility (private/unlisted/public) and Save/Publish.
  6. Capture the resulting video URL.

Metadata
--------
Point --metadata at the JSON that video_maker emits
(output/<slug>/<slug>_metadata.json: {title, description, tags[...]}). Explicit
--title / --description / --tags / --thumbnail override the JSON.

Safety
------
Default visibility is **private** — the upload never becomes public unless you
pass --visibility public. Use --keep-open to leave the window up for inspection.

Discovered selectors (2026 YouTube Studio UI, English labels)
-------------------------------------------------------------
  Create button        : ytcp-button#create-icon  (aria-label 'Create')
  Menu 'Upload videos' : tp-yt-paper-item  (text 'Upload video')
  File input           : input[type='file']  (set_input_files, no chooser click)
  Title  (contentedit) : #title-textarea #textbox
  Description          : #description-textarea #textbox
  Thumbnail input      : ytcp-thumbnails-compact-editor-uploader input[type='file']
  Audience 'not kids'  : tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']
  Show more            : ytcp-button#toggle-button  (text 'Show more')
  Tags input           : input[aria-label='Tags']  (comma-separated)
  Next                 : ytcp-button#next-button
  Visibility radios    : tp-yt-paper-radio-button[name='PRIVATE'|'UNLISTED'|'PUBLIC']
  Save/Publish         : ytcp-button#done-button

Usage
-----
  # From a video_maker metadata bundle
  python3 youtube_upload.py \
      --video    ../marketmaker/video_maker/output/SLUG/SLUG.mp4 \
      --metadata ../marketmaker/video_maker/output/SLUG/SLUG_metadata.json \
      --thumbnail ../marketmaker/video_maker/output/SLUG/SLUG_thumbnail.png \
      --visibility private --debug

  # Fully manual
  python3 youtube_upload.py --video clip.mp4 --title "Hi" --description "..." \
      --tags "a,b,c" --visibility unlisted

Exit codes: 0 ok · 2 bad args · 3 not logged in · 4 could not start upload ·
5 details step failed · 6 could not finish/publish · 7 blocked by a Google
"Verify it's you" challenge (complete it once in the window with --keep-open).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from gemini_common import (
    load_cookies, log, shot, hold,
    make_camoufox, prepare_persistent_page,
)

STUDIO_URL = "https://studio.youtube.com"


# --------------------------------------------------------------------------- #
# Deep dump (shadow-DOM piercing) — YouTube Studio is heavy web-components, so
# selectors sometimes need re-discovery. Mirrors notebooklm_gen.dump_ui.
# --------------------------------------------------------------------------- #
JS_DEEP_DUMP = r"""
() => {
  const out=[]; const seen=new Set();
  function walk(root){
    let els; try{els=root.querySelectorAll('*')}catch(e){return}
    for(const el of els){
      if(seen.has(el))continue; seen.add(el);
      const tag=el.tagName.toLowerCase();
      const role=el.getAttribute&&el.getAttribute('role');
      const aria=el.getAttribute&&el.getAttribute('aria-label');
      const id=el.id||'';
      const name=el.getAttribute&&el.getAttribute('name');
      const interesting=['button','input','textarea','a','tp-yt-paper-item',
        'tp-yt-paper-radio-button','ytcp-button'].includes(tag)
        ||role==='button'||role==='menuitem'||role==='radio'||aria;
      if(interesting){
        const r=el.getBoundingClientRect?el.getBoundingClientRect():{x:0,y:0,width:0,height:0};
        if(r.width>0&&r.height>0)
          out.push({tag,role:role||'',aria:(aria||'').slice(0,50),id,
            name:name||'',text:(el.innerText||el.textContent||'').trim().slice(0,40),
            x:Math.round(r.x),y:Math.round(r.y)});
      }
      if(el.shadowRoot)walk(el.shadowRoot);
    }
  }
  walk(document); return out;
}
"""


async def dump_ui(page, tag: str) -> None:
    try:
        items = await page.evaluate(JS_DEEP_DUMP)
    except Exception as e:  # noqa: BLE001
        log(f"  dump failed: {e}")
        return
    log(f"=== deep dump [{tag}]: {len(items)} ===")
    for i in items:
        log(f"  <{i['tag']}> id='{i['id']}' name='{i['name']}' "
            f"role='{i['role']}' aria='{i['aria']}' text='{i['text']}' @({i['x']},{i['y']})")


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
async def goto_retry(page, url: str, tries: int = 3) -> None:
    last = None
    for a in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  goto attempt {a + 1} failed: {str(e).splitlines()[0][:60]}")
            await page.wait_for_timeout(1500)
    if last:
        raise last


async def click_text(page, words: List[str], timeout_ms: int = 4000) -> bool:
    """Click the first visible button/menuitem/radio matching any word."""
    sel = ("button, [role=button], [role=menuitem], [role=radio], "
           "tp-yt-paper-item, ytcp-button, a")
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
            hay = ((await el.get_attribute("aria-label") or "") + " "
                   + (await el.inner_text() or "")).lower()
            if any(w.lower() in hay for w in words):
                await el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _first_visible(page, selectors: List[str], timeout_ms: int = 15_000):
    """Return the first visible locator among selectors, polling until timeout."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    return loc.first
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(500)
    return None


async def fill_contenteditable(page, loc, text: str) -> bool:
    """Set a YouTube Studio contenteditable (title/description) reliably: focus,
    select-all, delete, then type the new value."""
    try:
        await loc.click(timeout=4000)
        await page.wait_for_timeout(200)
        # macOS Camoufox -> Meta+A; also send Control+A as a cross-platform fallback.
        for combo in ("Meta+A", "Control+A"):
            try:
                await page.keyboard.press(combo)
            except Exception:  # noqa: BLE001
                pass
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(150)
        await page.keyboard.type(text, delay=2)
        await page.wait_for_timeout(300)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  fill_contenteditable failed: {str(e).splitlines()[0][:60]}")
        return False


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def load_metadata(args) -> dict:
    """Merge --metadata JSON with explicit CLI overrides. Returns
    {title, description, tags(list)}."""
    meta = {"title": "", "description": "", "tags": []}
    if args.metadata:
        p = Path(args.metadata).expanduser()
        if not p.is_file():
            log(f"  --metadata not found: {p}")
        else:
            data = json.loads(p.read_text(encoding="utf-8"))
            meta["title"] = (data.get("title") or "").strip()
            meta["description"] = data.get("description") or ""
            tags = data.get("tags") or []
            meta["tags"] = [t for t in tags if t]
    if args.title:
        meta["title"] = args.title
    if args.description:
        meta["description"] = args.description
    if args.tags:
        meta["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    # YouTube hard limits: title 100 chars, tags total <= 500 chars.
    if len(meta["title"]) > 100:
        log(f"  title >100 chars; truncating")
        meta["title"] = meta["title"][:100]
    meta["tags"] = _cap_tags(meta["tags"])
    return meta


def _cap_tags(tags: List[str], budget: int = 480) -> List[str]:
    out, used = [], 0
    for t in tags:
        add = len(t) + (1 if out else 0)
        if used + add > budget:
            break
        out.append(t)
        used += add
    return out


# --------------------------------------------------------------------------- #
# Upload flow
# --------------------------------------------------------------------------- #
# Studio dialogs render inside shadow DOM, which document.body.innerText does NOT
# capture — walk light + shadow trees to read the challenge text.
JS_ALL_TEXT = r"""
() => {
  const acc=[]; const seen=new Set();
  function walk(root){
    if(!root||seen.has(root))return; seen.add(root);
    const kids=root.childNodes||[];
    for(const n of kids){
      if(n.nodeType===3){ const t=(n.textContent||'').trim(); if(t)acc.push(t); }
      else if(n.nodeType===1){ if(n.shadowRoot) walk(n.shadowRoot); walk(n); }
    }
  }
  walk(document.body);
  return acc.join(' ').toLowerCase();
}
"""


async def verify_gate_present(page) -> bool:
    """True if Google's 'Verify it's you' identity challenge is on screen."""
    try:
        text = await page.evaluate(JS_ALL_TEXT)
    except Exception:  # noqa: BLE001
        text = ""
    return ("verify it's you" in text
            or "verify it’s you" in text
            or "confirm it's really you" in text
            or "confirm it’s really you" in text)


async def dismiss_overlays(page) -> None:
    """Close 'What's new' / announcement / cookie modals that overlay the
    dashboard and intercept the upload controls."""
    for txt in ("Got it", "Dismiss", "No thanks", "Skip", "Not now",
                "Continue", "I agree", "Accept all"):
        try:
            b = page.locator(
                f"ytcp-button:has-text('{txt}'), button:has-text('{txt}')")
            if await b.count() > 0 and await b.first.is_visible():
                await b.first.click(timeout=1500)
                await page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            continue


async def open_upload_dialog(page, video: Path, debug: bool) -> bool:
    """Open the upload dialog and feed it the (hidden) video file input."""
    await dismiss_overlays(page)

    # The topbar / dashboard exposes a direct "Upload videos" control that opens
    # the uploads dialog straight away — more reliable than the Create menu.
    opened = False
    for sel in ("ytcp-icon-button#upload-icon",
                "ytcp-button#upload-button",
                "button[aria-label='Upload videos']"):
        loc = page.locator(sel)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=4000)
                opened = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not opened:
        await click_text(page, ["Create"], timeout_ms=8000)
        await page.wait_for_timeout(800)
        opened = await click_text(page, ["Upload video"], timeout_ms=5000)

    await page.wait_for_timeout(1500)
    await dismiss_overlays(page)
    await shot(page, "yt_02_upload_dialog", debug)

    # The <input type=file> inside the uploads dialog is hidden (zero-sized);
    # set_input_files works on it directly, so wait for it to EXIST, not to be
    # visible.
    file_input = None
    deadline = time.time() + 15
    while time.time() < deadline:
        loc = page.locator(
            "ytcp-uploads-dialog input[type='file'], input[type='file']")
        try:
            if await loc.count() > 0:
                file_input = loc.first
                break
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(500)
    if file_input is None:
        log("  no file input found for upload")
        await dump_ui(page, "no file input")
        return False
    try:
        await file_input.set_input_files(str(video))
        log(f"  selected video file: {video.name}")
    except Exception as e:  # noqa: BLE001
        log(f"  set_input_files failed: {str(e).splitlines()[0][:70]}")
        return False
    await page.wait_for_timeout(4000)
    await shot(page, "yt_03_uploading", debug)
    return True


async def wait_details_ready(page, timeout_s: int, debug: bool) -> bool:
    """Wait for the Details step (title box) to appear."""
    title = await _first_visible(
        page,
        ["#title-textarea #textbox",
         "ytcp-social-suggestions-textbox#title-textarea #textbox",
         "#textbox[aria-label*='title' i]"],
        timeout_ms=timeout_s * 1000,
    )
    if title is None:
        log("  Details step (title box) never appeared")
        await dump_ui(page, "no details")
        await shot(page, "yt_04_no_details", True)
        return False
    log("  Details step ready")
    await shot(page, "yt_04_details", debug)
    return True


async def fill_details(page, meta: dict, thumbnail: Optional[Path],
                       made_for_kids: bool, debug: bool) -> bool:
    # Title
    title_box = await _first_visible(
        page, ["#title-textarea #textbox",
               "ytcp-social-suggestions-textbox#title-textarea #textbox"])
    if title_box is not None and meta["title"]:
        if await fill_contenteditable(page, title_box, meta["title"]):
            log(f"  title set ({len(meta['title'])} chars)")

    # Description
    desc_box = await _first_visible(
        page, ["#description-textarea #textbox",
               "ytcp-social-suggestions-textbox#description-textarea #textbox"])
    if desc_box is not None and meta["description"]:
        if await fill_contenteditable(page, desc_box, meta["description"]):
            log(f"  description set ({len(meta['description'])} chars)")

    # Thumbnail (optional; only after the input becomes available post-processing)
    if thumbnail is not None and thumbnail.is_file():
        thumb = page.locator(
            "ytcp-thumbnails-compact-editor-uploader input[type='file'], "
            "#file-loader input[type='file']")
        try:
            if await thumb.count() > 0:
                await thumb.first.set_input_files(str(thumbnail))
                log(f"  thumbnail set: {thumbnail.name}")
                await page.wait_for_timeout(2000)
            else:
                log("  thumbnail input not available yet; skipping")
        except Exception as e:  # noqa: BLE001
            log(f"  thumbnail upload failed: {str(e).splitlines()[0][:60]}")

    # Tags (behind "Show more")
    if meta["tags"]:
        await click_text(page, ["Show more"], timeout_ms=4000)
        await page.wait_for_timeout(800)
        tag_input = await _first_visible(
            page, ["input[aria-label='Tags']",
                   "#tags-container input#text-input",
                   "ytcp-form-input-container#tags-container input"])
        if tag_input is not None:
            try:
                await tag_input.click(timeout=3000)
                # YouTube commits a tag on comma; append trailing comma per tag.
                await page.keyboard.type(", ".join(meta["tags"]) + ",", delay=2)
                log(f"  tags set ({len(meta['tags'])})")
            except Exception as e:  # noqa: BLE001
                log(f"  tag entry failed: {str(e).splitlines()[0][:60]}")
        else:
            log("  tags input not found; skipping")

    # Audience — REQUIRED to advance.
    kids_name = ("VIDEO_MADE_FOR_KIDS_MFK" if made_for_kids
                 else "VIDEO_MADE_FOR_KIDS_NOT_MFK")
    radio = page.locator(f"tp-yt-paper-radio-button[name='{kids_name}']")
    try:
        if await radio.count() > 0:
            await radio.first.click(timeout=4000)
            log(f"  audience set: {'made for kids' if made_for_kids else 'not made for kids'}")
        else:
            # Fallback by label text.
            label = ("Yes, it's made for kids" if made_for_kids
                     else "No, it's not made for kids")
            if await click_text(page, [label], timeout_ms=4000):
                log(f"  audience set via label: {label}")
            else:
                log("  WARNING: could not set audience — Next may be blocked")
    except Exception as e:  # noqa: BLE001
        log(f"  audience click failed: {str(e).splitlines()[0][:60]}")

    await shot(page, "yt_05_details_filled", debug)
    return True


async def click_next(page, times: int, debug: bool) -> None:
    """Advance Details -> Elements -> Checks -> Visibility."""
    for i in range(times):
        nxt = page.locator("ytcp-button#next-button, #next-button")
        try:
            if await nxt.count() > 0 and await nxt.first.is_visible():
                await nxt.first.click(timeout=5000)
                log(f"  Next ({i + 1}/{times})")
                await page.wait_for_timeout(1500)
            else:
                log(f"  Next button not visible at step {i + 1}")
                await dump_ui(page, f"no next {i + 1}")
        except Exception as e:  # noqa: BLE001
            log(f"  Next click failed: {str(e).splitlines()[0][:60]}")
        await shot(page, f"yt_06_step_{i + 1}", debug)


async def set_visibility(page, visibility: str, debug: bool) -> None:
    name = {"private": "PRIVATE", "unlisted": "UNLISTED",
            "public": "PUBLIC"}.get(visibility.lower(), "PRIVATE")
    radio = page.locator(f"tp-yt-paper-radio-button[name='{name}']")
    try:
        if await radio.count() > 0:
            await radio.first.click(timeout=5000)
            log(f"  visibility set: {visibility}")
        else:
            label = {"private": "Private", "unlisted": "Unlisted",
                     "public": "Public"}[visibility.lower()]
            if await click_text(page, [label], timeout_ms=4000):
                log(f"  visibility set via label: {label}")
            else:
                log("  WARNING: visibility radio not found")
    except Exception as e:  # noqa: BLE001
        log(f"  visibility click failed: {str(e).splitlines()[0][:60]}")
    await shot(page, "yt_07_visibility", debug)


async def finish(page, debug: bool) -> Optional[str]:
    """Click Save/Publish (#done-button) and capture the resulting URL."""
    done = page.locator("ytcp-button#done-button, #done-button")
    try:
        if await done.count() > 0 and await done.first.is_visible():
            await done.first.click(timeout=6000)
            log("  clicked Save/Publish")
        else:
            log("  done-button not visible")
            await dump_ui(page, "no done")
            return None
    except Exception as e:  # noqa: BLE001
        log(f"  done click failed: {str(e).splitlines()[0][:60]}")
        return None

    # Confirmation dialog exposes the watch URL.
    await page.wait_for_timeout(4000)
    await shot(page, "yt_08_published", debug)
    url = None
    try:
        link = page.locator("a[href*='youtu.be/'], a[href*='watch?v='], "
                             "ytcp-video-info a, #share-url a")
        if await link.count() > 0:
            url = await link.first.get_attribute("href")
    except Exception:  # noqa: BLE001
        pass
    if not url:
        try:
            url = await page.evaluate(
                "() => { const a=[...document.querySelectorAll('a')]"
                ".find(x=>/youtu\\.be\\/|watch\\?v=/.test(x.href)); return a?a.href:'' }")
        except Exception:  # noqa: BLE001
            url = ""
    return url or None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run(args) -> int:
    video = Path(args.video).expanduser()
    if not video.is_file():
        log(f"ERROR: --video not found: {video}")
        return 2
    thumbnail = Path(args.thumbnail).expanduser() if args.thumbnail else None
    meta = load_metadata(args)
    if not meta["title"]:
        meta["title"] = video.stem.replace("-", " ").replace("_", " ").title()
        log(f"  no title given; using '{meta['title']}'")

    _, cookies = load_cookies(args.profile)

    async with make_camoufox(args.headless) as context:
        page = await prepare_persistent_page(context, cookies)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        await goto_retry(page, STUDIO_URL)
        await page.wait_for_timeout(4000)
        await shot(page, "yt_01_studio", args.debug)
        if "accounts.google.com" in page.url or "/ServiceLogin" in page.url:
            log("ERROR: not logged in (redirected to Google sign-in).")
            if args.keep_open:
                await hold(page)
            return 3

        # Google may interrupt sensitive actions (upload) with a "Verify it's
        # you" identity challenge. It cannot be auto-bypassed — the human must
        # complete it once in the window; the persistent profile is trusted
        # afterwards.
        if await verify_gate_present(page):
            log("BLOCKED: Google 'Verify it's you' security challenge is up.")
            await shot(page, "yt_verify_gate", True)
            if not args.keep_open:
                log("  Re-run with --keep-open and complete the verification "
                    "in the Camoufox window; then it won't ask again.")
                return 7
            log("  Complete the verification in the window — waiting up to "
                f"{args.verify_wait}s...")
            await click_text(page, ["Next", "Continue"], timeout_ms=4000)
            deadline = time.time() + args.verify_wait
            while time.time() < deadline and await verify_gate_present(page):
                await page.wait_for_timeout(3000)
            if await verify_gate_present(page):
                log("  Still gated after wait; aborting.")
                await hold(page)
                return 7
            log("  Verification cleared — continuing.")
            await goto_retry(page, STUDIO_URL)
            await page.wait_for_timeout(3000)

        if not await open_upload_dialog(page, video, args.debug):
            if args.keep_open:
                await hold(page)
            return 4

        if not await wait_details_ready(page, args.timeout, args.debug):
            if args.keep_open:
                await hold(page)
            return 5

        await fill_details(page, meta, thumbnail, args.made_for_kids, args.debug)
        # Details -> Video elements -> Checks -> Visibility = 3 Next clicks.
        await click_next(page, 3, args.debug)
        await set_visibility(page, args.visibility, args.debug)

        url = await finish(page, args.debug)
        if url:
            log(f"Done. Uploaded ({args.visibility}): {url}")
        else:
            log("Upload submitted, but could not read the video URL — "
                "check debug/yt_08_published.png and YouTube Studio.")
        if args.keep_open:
            await hold(page)
        return 0 if url else 6


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="default-release",
                   help="Firefox profile logged into YouTube (name / dir / path)")
    p.add_argument("--video", required=True, help="Path to the video file to upload")
    p.add_argument("--metadata", default="",
                   help="video_maker metadata JSON (title/description/tags)")
    p.add_argument("--thumbnail", default="", help="Custom thumbnail image (PNG/JPG)")
    p.add_argument("--title", default="", help="Override title")
    p.add_argument("--description", default="", help="Override description")
    p.add_argument("--tags", default="", help="Override tags (comma-separated)")
    p.add_argument("--visibility", default="private",
                   choices=["private", "unlisted", "public"],
                   help="Visibility after upload (default: private)")
    p.add_argument("--made-for-kids", action="store_true",
                   help="Mark as made for kids (default: NOT made for kids)")
    p.add_argument("--timeout", type=int, default=120,
                   help="Max seconds to wait for the Details step (default 120)")
    p.add_argument("--verify-wait", type=int, default=600,
                   help="Seconds to wait for you to clear a 'Verify it's you' "
                        "challenge in the window (with --keep-open; default 600)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--keep-open", action="store_true",
                   help="Leave the browser open at the end (Ctrl+C to quit)")
    p.add_argument("--debug", action="store_true",
                   help="Save debug/yt_*.png screenshots at each step")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
