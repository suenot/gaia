#!/usr/bin/env python3
"""
Generate MUSIC from a text prompt using an *authorized* Google session, driven
through Camoufox (anti-detect Firefox) + Playwright. Reuses the logged-in Google
cookies extracted from the user's Firefox profile (see gemini_common.py).

Two candidate surfaces are supported:

  (a) Gemini "Create music" tool  -- https://gemini.google.com/app
      Expand the composer, click the "+" (Upload & tools), toggle the
      "Create music" menuitemcheckbox, type a prompt, send, and download the
      resulting audio track (inline like "Create video" produced a video).

  (b) Google MusicFX            -- https://labs.google/fx/tools/music-fx
      A dedicated Labs studio: text prompt -> generated audio.

Pick the surface with --surface (gemini | musicfx | auto). "auto" (default)
tries Gemini first, then falls back to MusicFX.

Usage
-----
    python3 music_gen.py --prompt "upbeat lo-fi hip hop beat, mellow piano, rain"
    python3 music_gen.py --prompt-file prompts/song.txt --surface musicfx --debug
    python3 music_gen.py --prompt "ambient" --explore --keep-open   # inspect UI

Outputs
-------
  * Generated audio   -> ./output/music_<timestamp>.<wav|mp3|...>
  * Debug screenshots -> ./debug/music_NN_*.png   (use --debug)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from gemini_common import (
    OUTPUT_DIR,
    DEBUG_DIR,
    load_cookies,
    log,
    shot,
    is_logged_in,
    make_camoufox,
    prepare_persistent_page,
    wait_for_editor,
    fill_prompt,
    find_editor,
    hold,
)

GEMINI_URL = "https://gemini.google.com/app"
MUSICFX_URLS = [
    "https://labs.google/fx/tools/music-fx",
    "https://labs.google/fx/tools/music-fx-dj",
    "https://labs.google/fx/tools",
]

UPLOAD_TOOLS_BTN = "button[aria-label='Upload & tools']"

DOWNLOAD_WORDS = ["download", "save", "export", "скачать", "сохранить"]
MORE_WORDS = ["more", "options", "ещё", "еще", "overflow"]

# ---------------------------------------------------------------------------
# JS helpers
# ---------------------------------------------------------------------------

# Deep DOM dump that pierces shadow roots: lists clickable/labeled controls.
JS_DEEP_DUMP = r"""
() => {
  const out = [];
  const seen = new Set();
  function walk(root) {
    const els = root.querySelectorAll('*');
    for (const el of els) {
      if (seen.has(el)) continue;
      seen.add(el);
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute && el.getAttribute('role');
      const aria = el.getAttribute && el.getAttribute('aria-label');
      const type = el.getAttribute && el.getAttribute('type');
      const interesting = tag === 'button' || tag === 'input' || tag === 'audio'
        || tag === 'video' || role === 'button' || role === 'menuitemcheckbox'
        || role === 'menuitem' || aria
        || (el.getAttribute && el.getAttribute('mattooltip'));
      if (interesting) {
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {x:0,y:0,width:0,height:0};
        out.push({
          tag, role: role || '', aria: (aria||'').slice(0,60), type: type || '',
          text: (el.innerText || el.textContent || '').trim().slice(0,50),
          tip: (el.getAttribute && (el.getAttribute('mattooltip')||el.getAttribute('data-test-id'))) || '',
          x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
        });
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  return out;
}
"""

# Collect any <audio> sources plus likely audio download links across shadow DOM.
JS_COLLECT_AUDIO = r"""
() => {
  const out = [];
  const seen = new Set();
  function add(src, kind, w, h) {
    if (!src) return;
    if (out.find(o => o.src === src)) return;
    out.push({ src, kind, dur: 0 });
  }
  function walk(root) {
    const els = root.querySelectorAll('*');
    for (const el of els) {
      if (seen.has(el)) continue;
      seen.add(el);
      const tag = el.tagName.toLowerCase();
      if (tag === 'audio') {
        const s = el.currentSrc || el.src || '';
        let src = s;
        if (!src) {
          const so = el.querySelector('source');
          if (so) src = so.src;
        }
        if (src) out.push({ src, kind: 'audio', dur: el.duration || 0 });
      }
      if (tag === 'a') {
        const href = el.href || '';
        const dl = el.getAttribute('download');
        if (href && (dl !== null || /\.(wav|mp3|ogg|m4a|flac|aac)(\?|$)/i.test(href))) {
          out.push({ src: href, kind: 'a[download]', dur: 0 });
        }
      }
      if (tag === 'source') {
        const t = (el.type || '');
        if (/audio/i.test(t) && el.src) out.push({ src: el.src, kind: 'source', dur: 0 });
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  // de-dup
  const uniq = [];
  const s2 = new Set();
  for (const o of out) { if (!s2.has(o.src)) { s2.add(o.src); uniq.push(o); } }
  return uniq;
}
"""

# In-page fetch -> base64 (works for live blob: URLs and same-origin https).
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


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

async def deep_dump(page, tag: str) -> List[dict]:
    try:
        items = await page.evaluate(JS_DEEP_DUMP)
    except Exception as e:  # noqa: BLE001
        log(f"  deep_dump failed: {e}")
        return []
    vis = [i for i in items if i["w"] > 0 and i["h"] > 0]
    log(f"=== deep dump [{tag}] : {len(items)} interesting, {len(vis)} visible ===")
    for i in vis:
        log(f"  <{i['tag']}> role='{i['role']}' type='{i['type']}' aria='{i['aria']}' "
            f"text='{i['text']}' tip='{i['tip']}' @({i['x']},{i['y']} {i['w']}x{i['h']})")
    return vis


async def click_word_button(page, words: List[str], timeout_ms: int = 4000) -> bool:
    """Click first visible button/menuitem/link whose aria-label or text matches."""
    sel = "button, [role=button], [role=menuitem], [role=menuitemcheckbox], [role=option], a"
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
    """Close feature-promo dialogs without ever clicking a primary CTA."""
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
    for txt in ("Not now", "No thanks", "Maybe later", "Dismiss", "Skip", "Got it"):
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
    except Exception:  # noqa: BLE001
        pass


def _ext_from(src: str, ctype: str) -> str:
    s = src.lower()
    for e in ("wav", "mp3", "ogg", "m4a", "flac", "aac"):
        if f".{e}" in s:
            return e
    ct = (ctype or "").lower()
    if "wav" in ct:
        return "wav"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    if "ogg" in ct:
        return "ogg"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return "m4a"
    if "flac" in ct:
        return "flac"
    return "wav"


async def download_audio(page, audio: dict, debug: bool) -> Optional[Path]:
    """Download an audio source via context.request -> in-page fetch -> UI button."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    src = audio["src"]
    stamp = time.strftime("%Y%m%d_%H%M%S")

    # Method 1: APIRequestContext (carries cookies, bypasses CORS).
    if src.startswith("http"):
        try:
            resp = await page.context.request.get(src, timeout=120_000)
            if resp.ok:
                body = await resp.body()
                if len(body) > 2_000:
                    ct = resp.headers.get("content-type", "")
                    out = OUTPUT_DIR / f"music_{stamp}.{_ext_from(src, ct)}"
                    out.write_bytes(body)
                    log(f"  saved {out.name} via context.request ({len(body):,} bytes, ct={ct})")
                    return out
                log(f"  context.request returned only {len(body)} bytes")
            else:
                log(f"  context.request HTTP {resp.status}")
        except Exception as e:  # noqa: BLE001
            log(f"  context.request failed: {str(e).splitlines()[0][:80]}")

    # Method 2: in-page fetch (live blob: URLs).
    try:
        res = await page.evaluate(JS_FETCH_B64, src)
        if res.get("size", 0) > 2_000:
            out = OUTPUT_DIR / f"music_{stamp}.{_ext_from(src, res.get('type',''))}"
            out.write_bytes(base64.b64decode(res["b64"]))
            log(f"  saved {out.name} via in-page fetch ({res['size']:,} bytes, type={res.get('type')})")
            return out
    except Exception as e:  # noqa: BLE001
        log(f"  in-page fetch failed: {str(e).splitlines()[0][:80]}")
    return None


async def download_via_ui(page, debug: bool) -> Optional[Path]:
    """Last resort: click a Download control and capture the browser download."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        async with page.expect_download(timeout=45_000) as dl_info:
            clicked = await click_word_button(page, DOWNLOAD_WORDS)
            if not clicked:
                await click_word_button(page, MORE_WORDS)
                await page.wait_for_timeout(800)
                clicked = await click_word_button(page, DOWNLOAD_WORDS)
            if not clicked:
                raise RuntimeError("no download control found")
            await page.wait_for_timeout(800)
            # possible submenu (format/quality)
            await click_word_button(page, DOWNLOAD_WORDS + ["wav", "mp3", "audio", "original"])
        dl = await dl_info.value
        suggested = dl.suggested_filename or ""
        ext = _ext_from(suggested, "") if "." in suggested else "wav"
        out = OUTPUT_DIR / f"music_{stamp}.{ext}"
        await dl.save_as(str(out))
        log(f"  saved {out.name} via UI download (suggested={suggested})")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"  UI download failed: {str(e).splitlines()[0][:80]}")
        await shot(page, "music_91_download_failed", True)
    return None


async def collect_audio(page) -> List[dict]:
    try:
        return await page.evaluate(JS_COLLECT_AUDIO)
    except Exception:  # noqa: BLE001
        return []


async def wait_for_audio(page, timeout_s: int, quiet_s: float, debug: bool,
                         baseline: Optional[set] = None) -> List[dict]:
    """Poll until generated <audio> / audio links appear and stabilize.

    `baseline` is a set of audio srcs that existed BEFORE we requested generation
    (e.g. template-preview tracks in the 'Create music' gallery). Those are
    excluded so we only return the newly generated track.
    """
    baseline = baseline or set()
    deadline = time.time() + timeout_s
    last_change = None
    seen: dict = {}
    last_log = 0.0
    start = time.time()
    shot_taken = False
    while time.time() < deadline:
        auds = await collect_audio(page)
        changed = False
        for a in auds:
            key = a["src"]
            if key in baseline:
                continue
            if key not in seen:
                seen[key] = a
                changed = True
                log(f"  new audio[{a['kind']}]: dur={a.get('dur',0):.1f}s {key[:70]}...")
        if changed:
            last_change = time.time()
            if not shot_taken:
                await shot(page, "music_70_audio_appeared", debug)
                shot_taken = True
        if seen and last_change is not None and (time.time() - last_change) >= quiet_s:
            break
        if time.time() - last_log > 20:
            elapsed = int(time.time() - start)
            log(f"  ...still waiting for audio ({elapsed}s elapsed)")
            last_log = time.time()
            if debug and elapsed and elapsed % 60 < 3:
                await shot(page, f"music_wait_{elapsed}s", True)
        await page.wait_for_timeout(2000)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Surface (a): Gemini "Create music"
# ---------------------------------------------------------------------------

async def ensure_upload_button(page, attempts: int = 6) -> bool:
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


async def enable_create_music(page, debug: bool) -> Tuple[bool, bool]:
    """Toggle on the 'Create music' tool. Returns (found, opened_separate_studio).

    Some Gemini tools open inline (toggle stays in the composer); others launch a
    separate studio/new tab. We detect a new tab/page if one appears.
    """
    if not await _open_upload_menu(page):
        return False, False
    await shot(page, "music_11_tools_menu", debug)
    await deep_dump(page, "Upload & tools menu") if debug else None

    item = page.locator("[role=menuitemcheckbox]:has-text('Create music')")
    if await item.count() == 0:
        item = page.locator("[role=menuitem]:has-text('Create music')")
    if await item.count() == 0:
        item = page.locator("button:has-text('Create music')")
    if await item.count() == 0:
        log("  'Create music' tool NOT found in Upload & tools menu")
        await page.keyboard.press("Escape")
        return False, False

    ctx = page.context
    pages_before = len(ctx.pages)
    await item.first.click()
    await page.wait_for_timeout(1200)
    # Did a separate studio open in a new tab?
    opened_new = len(ctx.pages) > pages_before
    try:
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(400)
    log(f"  'Create music' tool toggled (new_tab={opened_new})")
    await shot(page, "music_12_music_tool", debug)
    return True, opened_new


async def submit_and_verify(page, prompt: str) -> bool:
    snippet = " ".join(prompt.split())[:25]
    btn = page.locator("button[aria-label='Send message']")
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


async def run_gemini(page, prompt: str, args) -> Tuple[int, Optional[Path]]:
    """Returns (status, saved_path). status: 0 ok, >0 specific failure code.

    Status 50 = surface unavailable (no Create music tool) -> caller may fall back.
    """
    editor, sel = await wait_for_editor(page)
    if editor is None:
        log("  [gemini] prompt editor not found.")
        await shot(page, "music_02_no_editor", True)
        return 4, None
    log(f"  [gemini] editor: {sel}")

    if not await ensure_upload_button(page):
        log("  [gemini] warning: 'Upload & tools' (+) button did not appear")
    await page.wait_for_timeout(400)
    await dismiss_dialogs(page)

    if args.explore:
        await deep_dump(page, "gemini fresh chat")

    found, opened_new = await enable_create_music(page, args.debug)
    if not found:
        log("  [gemini] 'Create music' not available on this account/UI.")
        return 50, None
    await dismiss_dialogs(page)

    # If a separate studio opened in a new tab, switch to it.
    if opened_new:
        await page.wait_for_timeout(1500)
        newp = page.context.pages[-1]
        log(f"  [gemini] switched to new tab: {newp.url}")
        await shot(newp, "music_13_studio_tab", args.debug)
        # Drive the studio generically.
        return await run_generic_studio(newp, prompt, args, tag="gemini-studio")

    # Inline flow: type prompt + send in the same composer.
    if not await fill_prompt(page, prompt):
        log("  [gemini] could not enter the prompt.")
        await shot(page, "music_14_fill_failed", True)
        return 4, None
    await dismiss_dialogs(page)
    await shot(page, "music_15_ready_to_send", args.debug)

    if args.explore and args.no_send:
        log("  --explore --no-send: stopping before send.")
        await deep_dump(page, "gemini ready to send")
        return 0, None

    # Baseline: template-preview audio already on the 'Create music' page; exclude
    # these so we only capture the newly generated track.
    baseline_auds = await collect_audio(page)
    baseline = {a["src"] for a in baseline_auds}
    log(f"  [gemini] baseline audio sources (templates): {len(baseline)}")

    if not await submit_and_verify(page, prompt):
        log("  [gemini] could not send the message.")
        await shot(page, "music_16_send_failed", True)
        return 4, None
    log("  [gemini] sent; waiting for music generation...")
    await shot(page, "music_17_sent", args.debug)

    auds = await wait_for_audio(page, timeout_s=args.timeout, quiet_s=args.quiet,
                                debug=args.debug, baseline=baseline)
    await shot(page, "music_80_result", args.debug)
    if not auds:
        log("  [gemini] no audio detected.")
        if args.explore:
            await deep_dump(page, "gemini no audio")
        return 5, None

    auds.sort(key=lambda a: (a.get("dur") or 0), reverse=True)
    log(f"  [gemini] detected {len(auds)} audio source(s); downloading best...")
    saved = await download_audio(page, auds[0], args.debug)
    if saved is None:
        saved = await download_via_ui(page, args.debug)
    return (0, saved) if saved else (5, None)


# ---------------------------------------------------------------------------
# Surface (b): MusicFX (and generic studio driver)
# ---------------------------------------------------------------------------

async def goto_url(page, url: str) -> bool:
    last_err = None
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  nav attempt {attempt+1} to {url} failed: {str(e).splitlines()[0][:60]}")
            await page.wait_for_timeout(1500)
    if last_err is not None:
        return False
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(2500)
    return True


async def find_textbox(page):
    """Find a prompt textbox in a generic studio (textarea or contenteditable)."""
    selectors = [
        "textarea",
        "input[type='text']",
        "div[contenteditable='true']",
        "[role='textbox']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            n = await loc.count()
            for i in range(n):
                el = loc.nth(i)
                if await el.is_visible():
                    return el, sel
        except Exception:  # noqa: BLE001
            continue
    return None, None


async def run_generic_studio(page, prompt: str, args, tag: str) -> Tuple[int, Optional[Path]]:
    """Drive a standalone music studio: find prompt box, type, find a Create/Generate
    button, click, wait for audio, download."""
    await dismiss_dialogs(page)
    if args.explore:
        await deep_dump(page, f"{tag} loaded")

    # Detect a sign-in wall.
    url = page.url
    if "accounts.google.com" in url or "/ServiceLogin" in url:
        log(f"  [{tag}] hit a sign-in wall: {url}")
        await shot(page, "music_30_signin_wall", True)
        return 3, None

    tb, sel = await find_textbox(page)
    if tb is None:
        log(f"  [{tag}] no prompt textbox found.")
        await shot(page, "music_31_no_textbox", True)
        if args.explore:
            await deep_dump(page, f"{tag} no textbox")
        return 50, None
    log(f"  [{tag}] textbox: {sel}")

    try:
        await tb.click(timeout=4000)
    except Exception:  # noqa: BLE001
        pass
    try:
        await tb.fill(prompt, timeout=8000)
    except Exception:  # noqa: BLE001
        try:
            await tb.type(prompt, delay=10)
        except Exception as e:  # noqa: BLE001
            log(f"  [{tag}] could not enter prompt: {str(e).splitlines()[0][:60]}")
            return 4, None
    await page.wait_for_timeout(500)
    await shot(page, "music_40_prompt_filled", args.debug)

    if args.explore and args.no_send:
        log("  --explore --no-send: stopping before generate.")
        await deep_dump(page, f"{tag} ready")
        return 0, None

    # Click a generate/create button.
    gen_words = ["create", "generate", "make", "submit", "run", "play", "создать", "сгенерировать"]
    clicked = await click_word_button(page, gen_words)
    if not clicked:
        # try pressing Enter in the textbox
        try:
            await tb.press("Enter")
            clicked = True
            log(f"  [{tag}] pressed Enter to submit")
        except Exception:  # noqa: BLE001
            pass
    if not clicked:
        log(f"  [{tag}] no generate button found.")
        await shot(page, "music_41_no_generate", True)
        if args.explore:
            await deep_dump(page, f"{tag} no generate button")
        return 50, None
    log(f"  [{tag}] generation requested; waiting for audio...")
    await shot(page, "music_50_generating", args.debug)

    auds = await wait_for_audio(page, timeout_s=args.timeout, quiet_s=args.quiet, debug=args.debug)
    await shot(page, "music_80_result", args.debug)
    if not auds:
        log(f"  [{tag}] no audio detected.")
        if args.explore:
            await deep_dump(page, f"{tag} no audio")
        return 5, None

    auds.sort(key=lambda a: (a.get("dur") or 0), reverse=True)
    log(f"  [{tag}] detected {len(auds)} audio source(s); downloading best...")
    saved = await download_audio(page, auds[0], args.debug)
    if saved is None:
        saved = await download_via_ui(page, args.debug)
    return (0, saved) if saved else (5, None)


async def run_musicfx(page, prompt: str, args) -> Tuple[int, Optional[Path]]:
    for url in MUSICFX_URLS:
        log(f"  [musicfx] navigating to {url}")
        if not await goto_url(page, url):
            continue
        await shot(page, "music_20_musicfx_loaded", args.debug)
        status, saved = await run_generic_studio(page, prompt, args, tag="musicfx")
        if status == 0:
            return 0, saved
        if status == 3:
            # sign-in wall: trying other URLs won't help
            return 3, None
        # status 50 (no textbox/generate) -> try next candidate URL
        if status not in (50,):
            return status, saved
    log("  [musicfx] none of the candidate URLs exposed a usable studio.")
    return 50, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args) -> int:
    profile_path, cookies = load_cookies(args.profile)

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        log("ERROR: no prompt provided (use --prompt or --prompt-file)")
        return 2
    log(f"Firefox profile: {profile_path.name}; {len(cookies)} cookies")
    log(f"Surface: {args.surface}")
    log(f"Prompt ({len(prompt)} chars): {prompt[:80].replace(chr(10), ' ')}...")

    async with make_camoufox(args.headless) as context:
        page = await prepare_persistent_page(context, cookies)

        saved: Optional[Path] = None
        final_status = 5

        order = []
        if args.surface == "gemini":
            order = ["gemini"]
        elif args.surface == "musicfx":
            order = ["musicfx"]
        else:  # auto
            order = ["gemini", "musicfx"]

        for surface in order:
            log(f"==== Trying surface: {surface} ====")
            if surface == "gemini":
                if not await goto_url(page, GEMINI_URL):
                    log("  [gemini] navigation failed.")
                    continue
                await shot(page, "music_01_gemini_loaded", args.debug)
                if not await is_logged_in(page):
                    log(f"  [gemini] NOT logged in. url: {page.url}")
                    await shot(page, "music_02_not_logged_in", True)
                    continue
                log("  [gemini] logged in ✓")
                status, sp = await run_gemini(page, prompt, args)
            else:  # musicfx
                status, sp = await run_musicfx(page, prompt, args)

            final_status = status
            if status == 0 and sp is not None:
                saved = sp
                break
            if status == 0 and sp is None:
                # explore/no-send path
                break
            if status == 50:
                log(f"  surface '{surface}' unavailable; "
                    + ("falling back..." if surface != order[-1] else "no more surfaces."))
                continue
            # other failure (4/5/3): for auto, still try fallback
            if args.surface == "auto" and surface != order[-1]:
                log(f"  surface '{surface}' failed (status {status}); trying fallback...")
                continue
            break

        if saved is not None:
            log(f"SUCCESS. Saved audio -> {saved}")
        else:
            log(f"FAILED. No audio downloaded (last status {final_status}). "
                "See ./debug/music_*.png for evidence.")

        if args.keep_open:
            await hold(page)

        return 0 if saved is not None else (final_status or 5)


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompt", default="", help="Inline music prompt")
    p.add_argument("--prompt-file", default="", help="Read the prompt from this file")
    p.add_argument("--surface", choices=["gemini", "musicfx", "auto"], default="auto",
                   help="Which surface to use (default: auto = gemini then musicfx)")
    p.add_argument("--profile", default="default-release",
                   help="Firefox profile name/dir/path logged into Google (default: default-release)")
    p.add_argument("--timeout", type=int, default=300, help="Max seconds to wait for audio (default 300)")
    p.add_argument("--quiet", type=float, default=8.0,
                   help="Seconds of no change before generation is considered done (default 8)")
    p.add_argument("--headless", action="store_true", help="Run headless (visible is more reliable)")
    p.add_argument("--keep-open", action="store_true", help="Keep the browser open after finishing")
    p.add_argument("--debug", action="store_true", help="Save step screenshots to ./debug (music_NN_*.png)")
    p.add_argument("--explore", action="store_true", help="Deep-dump UI (pierces shadow DOM) to adapt selectors")
    p.add_argument("--no-send", action="store_true", help="With --explore: stop before sending/generating")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
