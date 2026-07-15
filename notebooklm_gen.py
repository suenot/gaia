#!/usr/bin/env python3
"""
Automate NotebookLM (https://notebooklm.google.com) end-to-end through Camoufox +
Playwright, reusing the persistent Camoufox Google session (see gemini_common:
make_camoufox / prepare_persistent_page).

Capabilities
------------
  * Create a notebook (or operate on an existing one with --notebook URL).
  * Add sources, any combination of:
      --source-text "..."            paste text
      --source-url URL               add a website/YouTube URL  (repeatable)
      --source-file PATH             add a file (repeatable; .txt/.md/.csv are
                                     pasted, others uploaded via the file chooser)
      --discover "query"             let NotebookLM web-search & add sources
  * Generate artifacts via their Customize popover, in a chosen language and with
    a steering prompt:
      --audio                        Audio Overview   (--instructions, --audio-format, --audio-length)
      --slides                       Slide Deck       (--slides-prompt)
      --language "Russian"           output language for the artifacts
  * Download the generated artifacts to ./output/.

Discovered selectors (2026 NotebookLM "Studio" UI, English aria-labels)
-----------------------------------------------------------------------
  Home create        : button[aria-label='Create new notebook']
  Add-source chips    : button:has-text('Copied text'|'Upload files'|'Websites'|'Drive')
  Paste text          : textarea[aria-label='Pasted text']  (+ button 'Insert')
  Discover sources    : textarea[aria-label='Discover sources based on the inputted query']
  Studio customize    : button[aria-label='Customize Audio Overview' | 'Customize Slide Deck' ...]
  Popover (audio)     : format tiles 'Deep Dive'/'Brief'/'Critique'/'Debate',
                        'Choose language' dropdown, length 'Short'/'Default'/'Long',
                        textarea[aria-label="What should the AI hosts focus on in this episode?"],
                        button 'Generate'
  Popover (slides)    : textarea[aria-label='Describe the slide deck you want to create'], 'Generate'

Usage
-----
  python3 notebooklm_gen.py --source-text "..." --audio --language Russian \
      --instructions "Focus on the engineering challenges"
  python3 notebooklm_gen.py --discover "history of the Voyager program" --audio --slides
  python3 notebooklm_gen.py --source-file paper.pdf --slides --slides-prompt "10 concise slides"
  python3 notebooklm_gen.py --notebook <url> --download-only        # grab ready artifacts
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
    OUTPUT_DIR, load_cookies, log, shot, hold,
    make_camoufox, prepare_persistent_page,
)

HOME = "https://notebooklm.google.com"

SAMPLE_TEXT = (
    "The Voyager 1 space probe, launched by NASA on September 5, 1977, is the "
    "most distant human-made object from Earth. After flybys of Jupiter and "
    "Saturn it crossed into interstellar space in 2012. It carries the Golden "
    "Record, a phonograph record of sounds and images portraying life on Earth, "
    "and its radioisotope generators should power at least one instrument into "
    "the late 2020s."
)

# --------------------------------------------------------------------------- #
# Deep dump (shadow-DOM piercing) for discovering controls.
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
      const interesting=['button','audio','input','textarea','a','mat-select','mat-option'].includes(tag)
        ||role==='button'||role==='tab'||role==='menuitem'||role==='option'||aria;
      if(interesting){
        const r=el.getBoundingClientRect?el.getBoundingClientRect():{x:0,y:0,width:0,height:0};
        if(r.width>0&&r.height>0)
          out.push({tag,role:role||'',aria:(aria||'').slice(0,60),
            text:(el.innerText||el.textContent||'').trim().slice(0,45),
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
        log(f"  <{i['tag']}> role='{i['role']}' aria='{i['aria']}' text='{i['text']}' @({i['x']},{i['y']})")


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
    """Click the first visible button/role=button/menuitem matching any word."""
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
            hay = ((await el.get_attribute("aria-label") or "") + " " + (await el.inner_text() or "")).lower()
            if any(w.lower() in hay for w in words):
                await el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def dismiss_dialogs(page) -> None:
    for txt in ("Got it", "Not now", "No thanks", "Maybe later", "Skip", "Dismiss"):
        b = page.locator(f"button:has-text('{txt}')")
        try:
            if await b.count() > 0 and await b.first.is_visible():
                await b.first.click(timeout=1500)
                await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Login + notebook
# --------------------------------------------------------------------------- #
async def open_home(page, debug: bool) -> bool:
    log(f"Navigating to {HOME}")
    await goto_retry(page, HOME)
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.wait_for_selector("button[aria-label='Create new notebook']",
                                     state="visible", timeout=30_000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(2000)
    await shot(page, "nlm_01_home", debug)
    if "accounts.google.com" in page.url or "signin" in page.url:
        log(f"ERROR: sign-in wall: {page.url}")
        return False
    if await page.locator("button[aria-label='Create new notebook']").count() == 0:
        log("WARNING: 'Create new notebook' not found — may not be logged in.")
        return False
    log("Logged in ✓ (NotebookLM home)")
    return True


async def _click_create_notebook(page) -> bool:
    """Click a *visible* 'Create new notebook' button (there can be duplicates)."""
    for sel in ("button[aria-label='Create new notebook']", "button:has-text('Create new')",
                "button[aria-label='Create notebook']", "button:has-text('Create notebook')"):
        loc = page.locator(sel)
        for i in range(await loc.count()):
            b = loc.nth(i)
            try:
                if await b.is_visible():
                    await b.click(timeout=4000)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


async def create_notebook(page, debug: bool) -> bool:
    for attempt in range(3):
        if "/notebook/" in page.url or await page.locator("button:has-text('Copied text')").count() > 0:
            break
        if not await _click_create_notebook(page):
            log("ERROR: create-notebook button not found.")
            return False
        # Wait for the notebook to actually open (URL -> /notebook/...) or the
        # add-source dialog to appear.
        for _ in range(25):
            await page.wait_for_timeout(1000)
            if "/notebook/" in page.url or await page.locator("button:has-text('Copied text')").count() > 0:
                break
        if "/notebook/" in page.url or await page.locator("button:has-text('Copied text')").count() > 0:
            break
        log(f"  create attempt {attempt + 1}: notebook didn't open, retrying...")
        await page.wait_for_timeout(1500)
    ok = "/notebook/" in page.url or await page.locator("button:has-text('Copied text')").count() > 0
    await page.wait_for_timeout(1500)
    await shot(page, "nlm_02_notebook_created", debug)
    log(f"  notebook url: {page.url} (opened={ok})")
    return ok


async def ensure_add_source_dialog(page) -> bool:
    if await page.locator("button:has-text('Copied text')").count() > 0:
        return True
    add = page.locator("button:has-text('Add sources'), button[aria-label*='Add source' i]")
    try:
        if await add.count() > 0 and await add.first.is_visible():
            await add.first.click(timeout=4000)
            await page.wait_for_selector("button:has-text('Copied text')", state="visible", timeout=10_000)
            return True
    except Exception:  # noqa: BLE001
        pass
    return await page.locator("button:has-text('Copied text')").count() > 0


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
async def add_source_text(page, text: str, debug: bool) -> bool:
    if not await ensure_add_source_dialog(page):
        log("ERROR: add-source dialog not available."); return False
    await page.locator("button:has-text('Copied text')").first.click()
    ta = page.locator("textarea[aria-label='Pasted text'], textarea[placeholder*='Paste' i]")
    try:
        await ta.first.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        ta = page.locator("textarea")
    await ta.first.fill(text)
    log(f"  pasted {len(text)} chars")
    await page.wait_for_timeout(600)
    if not await click_text(page, ["Insert"]):
        log("ERROR: 'Insert' not found."); return False
    return await _wait_source(page, debug)


async def add_source_url(page, url: str, debug: bool) -> bool:
    if not await ensure_add_source_dialog(page):
        return False
    await page.locator("button:has-text('Websites')").first.click()
    await page.wait_for_timeout(1500)
    field = None
    for sel in ("input[type='url']", "textarea[aria-label*='URL' i]", "input[aria-label*='URL' i]",
                "input[placeholder*='URL' i]", "textarea[placeholder*='URL' i]", "textarea", "input.mat-mdc-input-element"):
        loc = page.locator(sel)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                field = loc.first; break
        except Exception:  # noqa: BLE001
            continue
    if field is None:
        log("ERROR: URL field not found."); return False
    await field.fill(url)
    log(f"  entered URL: {url}")
    await page.wait_for_timeout(500)
    if not await click_text(page, ["Insert", "Add"]):
        log("ERROR: submit URL failed."); return False
    return await _wait_source(page, debug, timeout_s=150)


async def add_source_file(page, path: Path, debug: bool) -> bool:
    if not await ensure_add_source_dialog(page):
        return False
    up = page.locator("button:has-text('Upload files')")
    try:
        async with page.expect_file_chooser(timeout=10_000) as fc:
            await up.first.click()
        chooser = await fc.value
        await chooser.set_files(str(path))
        log(f"  uploaded file: {path.name}")
    except Exception as e:  # noqa: BLE001
        log(f"  file chooser failed: {str(e).splitlines()[0]}"); return False
    return await _wait_source(page, debug, timeout_s=180)


async def add_source_discover(page, query: str, debug: bool, max_add: int = 10) -> bool:
    """Use NotebookLM's 'Discover sources' web search to find & add sources."""
    box = page.locator("textarea[aria-label='Discover sources based on the inputted query']")
    if await box.count() == 0:
        log("ERROR: Discover sources box not found."); return False
    await box.first.click()
    await box.first.fill(query)
    log(f"  discover query: {query}")
    await page.wait_for_timeout(500)
    await box.first.press("Enter")
    await shot(page, "nlm_03_discover_sent", debug)
    # Results render with checkboxes; wait for them, then Add/Import.
    log("  waiting for discovered sources...")
    deadline = time.time() + 120
    while time.time() < deadline:
        if await page.locator("button:has-text('Add'), button:has-text('Import'), button:has-text('Insert')").count() > 0:
            break
        await page.wait_for_timeout(2000)
    await dismiss_dialogs(page)
    await shot(page, "nlm_03_discover_results", debug)
    # Add the discovered sources (button label varies: Add / Import / Insert).
    if not await click_text(page, ["Import", "Add sources", "Add", "Insert"]):
        log("  WARNING: could not find an Add button for discovered sources.")
    return await _wait_source(page, debug, timeout_s=150)


async def _wait_source(page, debug: bool, timeout_s: int = 120) -> bool:
    sel = ("mat-checkbox, [role='checkbox'], .single-source-container, .source-container, "
           "[class*='source-item' i], [data-test-id*='source' i]")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if await page.locator(sel).count() > 0:
                await page.wait_for_timeout(2500)
                await shot(page, "nlm_04_source_added", debug)
                log("  source ingested ✓")
                return True
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1500)
    log("  WARNING: source row not detected within timeout.")
    await shot(page, "nlm_04_source_timeout", True)
    return False


# --------------------------------------------------------------------------- #
# Artifact generation (Customize popover)
# --------------------------------------------------------------------------- #
# Map a few language names to their likely native option labels in NotebookLM.
LANG_ALIASES = {
    "russian": ["Russian", "Русский"],
    "english": ["English"],
    "spanish": ["Spanish", "Español"],
    "german": ["German", "Deutsch"],
    "french": ["French", "Français"],
}


async def _set_language(page, language: str, debug: bool = False) -> None:
    """Set the 'Choose language' dropdown inside an open Customize popover."""
    if not language:
        return
    trigger = None
    for sel in ("mat-select", "[role='combobox']", "div.mat-mdc-select-trigger"):
        loc = page.locator(sel)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                trigger = loc.first; break
        except Exception:  # noqa: BLE001
            continue
    if trigger is None:
        log("  (language dropdown not found; leaving default)"); return
    names = LANG_ALIASES.get(language.lower(), [language])
    # Skip the whole open-select-click dance if the target language is already
    # the current value — the dropdown shows it as the trigger's text.
    try:
        current = (await trigger.inner_text()).strip()
    except Exception:  # noqa: BLE001
        current = ""
    if current and any(current.lower() == n.lower() for n in names):
        log(f"  language already {current}; skipping select")
        return
    try:
        await trigger.click(timeout=3000)
        await page.wait_for_timeout(900)
        if debug:
            try:
                opts = await page.evaluate(
                    "() => Array.from(document.querySelectorAll(\"mat-option, [role='option']\"))"
                    ".map(o => (o.innerText||'').trim()).filter(Boolean).slice(0,40)"
                )
                log(f"  language options: {opts}")
            except Exception:  # noqa: BLE001
                pass
        for name in names:
            opt = page.locator(f"mat-option:has-text('{name}'), [role='option']:has-text('{name}')")
            if await opt.count() > 0:
                await opt.first.click(timeout=3000)
                log(f"  language set to {name}")
                return
        log(f"  language '{language}' not in list; leaving default")
        await page.keyboard.press("Escape")
    except Exception as e:  # noqa: BLE001
        log(f"  language set failed: {str(e).splitlines()[0][:50]}")


async def generate_artifact(page, kind: str, language: str, instructions: str,
                            fmt: str, length: str, debug: bool) -> bool:
    """kind: 'audio' (Audio Overview) or 'slides' (Slide Deck)."""
    label = "Customize Audio Overview" if kind == "audio" else "Customize Slide Deck"
    btn = page.locator(f"button[aria-label='{label}']")
    if await btn.count() == 0:
        log(f"ERROR: '{label}' button not found in Studio.");
        await dump_ui(page, f"no {label}")
        return False
    await btn.first.click()
    await page.wait_for_timeout(2000)
    await shot(page, f"nlm_05_{kind}_popover", debug)

    # Language (both popovers expose 'Choose language').
    await _set_language(page, language, debug)

    if kind == "audio":
        if fmt:
            await click_text(page, [fmt])          # Deep Dive / Brief / Critique / Debate
        if length:
            await click_text(page, [length])       # Short / Default / Long
        if instructions:
            ta = page.locator("textarea[aria-label*='focus on in this episode' i]")
            if await ta.count() > 0:
                await ta.first.fill(instructions)
                log("  set audio focus instructions")
    else:  # slides
        if instructions:
            ta = page.locator("textarea[aria-label*='Describe the slide deck' i]")
            if await ta.count() > 0:
                await ta.first.fill(instructions)
                log("  set slide-deck description")

    await page.wait_for_timeout(500)
    await shot(page, f"nlm_05_{kind}_ready", debug)
    # Click Generate (the popover's primary button).
    gen = page.locator("button:has-text('Generate')")
    if await gen.count() == 0:
        log("ERROR: Generate button not found in popover.");
        await dump_ui(page, f"{kind} popover no-generate")
        return False
    await gen.last.click()
    log(f"  {kind}: Generate clicked — generation started (a few minutes)...")
    await page.wait_for_timeout(3000)
    await dismiss_dialogs(page)
    await shot(page, f"nlm_06_{kind}_generating", debug)
    return True


# --------------------------------------------------------------------------- #
# Wait for ready + download
# --------------------------------------------------------------------------- #
JS_MEDIA = r"""
() => {
  const out=[]; const seen=new Set();
  function walk(root){
    let els; try{els=root.querySelectorAll('audio, source, video')}catch(e){els=[]}
    for(const el of els){
      if(seen.has(el))continue; seen.add(el);
      const src = el.currentSrc || el.src || '';
      if(src) out.push(src);
    }
    let all; try{all=root.querySelectorAll('*')}catch(e){all=[]}
    for(const el of all){ if(el.shadowRoot) walk(el.shadowRoot); }
  }
  walk(document);
  return out;
}
"""


async def wait_artifact_ready(page, kind: str, timeout_s: int, debug: bool) -> bool:
    """Wait until the artifact card reports ready (a Play / Download / More control
    appears, or 'Generating...' text disappears)."""
    label_word = "Audio Overview" if kind == "audio" else "Slide Deck"
    deadline = time.time() + timeout_s
    last = 0.0
    start = time.time()
    while time.time() < deadline:
        # A ready AUDIO card exposes a Play control (independent of other cards
        # still generating). A ready SLIDE deck no longer shows its "Generating
        # Slide Deck" placeholder and exposes an artifact More/open control.
        if kind == "audio":
            ready = await page.locator("button[aria-label='Play']").count() > 0
        else:
            try:
                body = (await page.evaluate("() => document.body.innerText || ''")).lower()
            except Exception:  # noqa: BLE001
                body = ""
            ready = ("generating slide deck" not in body) and (await _artifact_more_button(page, "slides") is not None)
        if ready and (time.time() - start) > 8:
            log(f"  {kind} appears ready")
            await shot(page, f"nlm_07_{kind}_ready", debug)
            return True
        now = time.time()
        if now - last > 20:
            log(f"  ...waiting for {kind} ({int(now - start)}s)")
            last = now
            if debug and int(now - start) % 60 < 2:
                await shot(page, f"nlm_wait_{kind}_{int(now-start)}s", True)
            await dismiss_dialogs(page)
        await page.wait_for_timeout(3000)
    log(f"  {kind} not confirmed ready within timeout.")
    await dump_ui(page, f"{kind} wait timeout")
    await shot(page, f"nlm_07_{kind}_timeout", True)
    return False


JS_FETCH_B64 = r"""
async (src) => {
  const r = await fetch(src);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const b = await r.blob(); const buf = await b.arrayBuffer();
  const bytes = new Uint8Array(buf); let bin=''; const ch=0x8000;
  for (let i=0;i<bytes.length;i+=ch) bin += String.fromCharCode.apply(null, bytes.subarray(i,i+ch));
  return { b64: btoa(bin), type: b.type||'', size: bytes.length };
}
"""


def _ext(kind: str, ctype: str, src: str) -> str:
    ct = (ctype or "").lower(); s = (src or "").lower()
    if kind == "audio":
        if "wav" in ct or ".wav" in s: return "wav"
        return "mp3"
    # slides
    if "pdf" in ct or ".pdf" in s: return "pdf"
    if "presentation" in ct or ".pptx" in s: return "pptx"
    return "pdf"


async def _play_button_rows(page) -> list:
    """Y-centres of all visible Play buttons (each marks an *audio* artifact row)."""
    ys = []
    play = page.locator("button[aria-label='Play']")
    for i in range(await play.count()):
        try:
            if not await play.nth(i).is_visible():
                continue
            box = await play.nth(i).bounding_box()
            if box:
                ys.append(box["y"] + box["height"] / 2)
        except Exception:  # noqa: BLE001
            continue
    return ys


async def _artifact_more_button(page, kind: str):
    """Return the More(⋮) button of the wanted *artifact card* in the Studio panel.

    Audio: the More right after a Play control. Slides/other: a Studio-side More
    that is NOT on the same row as ANY Play button (every audio card has its own
    Play, so excluding all Play rows leaves the non-audio artifacts)."""
    if kind == "audio":
        play = page.locator("button[aria-label='Play']")
        if await play.count() > 0:
            more = play.first.locator("xpath=following::button[@aria-label='More'][1]")
            if await more.count() > 0:
                return more.first
        return None

    play_ys = await _play_button_rows(page)
    mores = page.locator("button[aria-label='More']")
    cands = []
    for i in range(await mores.count()):
        b = mores.nth(i)
        try:
            if not await b.is_visible():
                continue
            box = await b.bounding_box()
            if not box or box["x"] < 1150:   # Studio panel only (right side)
                continue
            my = box["y"] + box["height"] / 2
            if any(abs(my - py) < 28 for py in play_ys):
                continue  # same row as a Play -> it's an audio card, skip
            cands.append((box["y"], b))
        except Exception:  # noqa: BLE001
            continue
    cands.sort(key=lambda t: t[0])  # top-most non-audio Studio artifact (the slide deck)
    return cands[0][1] if cands else None


MIN_BYTES = 20_000  # anything smaller is a failed/empty download


async def _grab_download(page, dl, kind: str):
    """Materialize a Playwright download reliably. Prefer fetching its URL with
    context.request (avoids Camoufox's flaky browser 'could not be saved' temp
    write); fall back to save_as with a size check. Returns (bytes, ext) or (None, ext)."""
    suggested = dl.suggested_filename or ""
    ext = suggested.rsplit(".", 1)[-1] if "." in suggested else _ext(kind, "", suggested)
    url = dl.url or ""
    if url.startswith("http"):
        try:
            resp = await page.context.request.get(url, timeout=120_000)
            if resp.ok:
                body = await resp.body()
                if len(body) > MIN_BYTES:
                    return body, (ext or _ext(kind, resp.headers.get("content-type", ""), url))
        except Exception:  # noqa: BLE001
            pass
    try:
        tmp = OUTPUT_DIR / f".tmp_{int(time.time()*1000)}"
        await dl.save_as(str(tmp))
        if tmp.exists() and tmp.stat().st_size > MIN_BYTES:
            data = tmp.read_bytes()
            tmp.unlink()
            return data, ext
        if tmp.exists():
            tmp.unlink()
    except Exception:  # noqa: BLE001
        pass
    return None, ext


async def _download_via_audio_src(page, stamp: str) -> Optional[Path]:
    """Audio only: click Play to instantiate <audio>, then fetch its src directly
    (context.request for http, in-page fetch for blob:). No browser download, so
    the Camoufox 'could not be saved' temp dialog never appears."""
    try:
        play = page.locator("button[aria-label='Play']")
        if await play.count() > 0:
            await play.first.click(timeout=4000)
            await page.wait_for_timeout(3000)
    except Exception:  # noqa: BLE001
        pass
    try:
        srcs = await page.evaluate(JS_MEDIA)
    except Exception:  # noqa: BLE001
        srcs = []
    for src in srcs:
        try:
            if src.startswith("http"):
                resp = await page.context.request.get(src, timeout=120_000)
                if resp.ok:
                    body = await resp.body()
                    if len(body) > MIN_BYTES:
                        out = OUTPUT_DIR / f"notebooklm_audio_{stamp}.{_ext('audio', resp.headers.get('content-type',''), src)}"
                        out.write_bytes(body)
                        log(f"  saved {out.name} via <audio> http src ({len(body):,} bytes)")
                        return out
            elif src.startswith("blob:"):
                res = await page.evaluate(JS_FETCH_B64, src)
                if res.get("size", 0) > MIN_BYTES:
                    out = OUTPUT_DIR / f"notebooklm_audio_{stamp}.{_ext('audio', res.get('type',''), src)}"
                    out.write_bytes(base64.b64decode(res["b64"]))
                    log(f"  saved {out.name} via <audio> blob ({res['size']:,} bytes)")
                    return out
        except Exception:  # noqa: BLE001
            pass
    return None


async def _download_via_more_menu(page, kind: str, stamp: str, debug: bool) -> Optional[Path]:
    """Open the artifact card's More(⋮) menu and pick a Download item, fetching the
    bytes via download.url/context.request (size-checked)."""
    more = await _artifact_more_button(page, kind)
    if more is None:
        return None
    try:
        async with page.expect_download(timeout=45_000) as dl_info:
            await more.click(timeout=4000)
            await page.wait_for_timeout(1000)
            if debug:
                await dump_ui(page, f"{kind} more-menu")
            items = (["Download PDF", "Download PowerPoint", "Download"]
                     if kind == "slides" else ["Download"])
            if not await click_text(page, items):
                raise RuntimeError("no Download item in More menu")
            await page.wait_for_timeout(600)
        dl = await dl_info.value
        data, ext = await _grab_download(page, dl, kind)
        if data:
            out = OUTPUT_DIR / f"notebooklm_{kind}_{stamp}.{ext}"
            out.write_bytes(data)
            log(f"  saved {out.name} via card More->Download ({len(data):,} bytes)")
            return out
        log("  More->Download produced no usable bytes.")
    except Exception as e:  # noqa: BLE001
        log(f"  More->Download failed: {str(e).splitlines()[0][:70]}")
    try:
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    return None


async def download_artifact(page, kind: str, debug: bool) -> Optional[Path]:
    """Download the artifact. Audio: prefer the <audio> src (download-free, avoids
    the Camoufox temp-save dialog); slides: the card More -> 'Download PDF'."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    if kind == "audio":
        p = await _download_via_audio_src(page, stamp)
        if p:
            return p
        p = await _download_via_more_menu(page, "audio", stamp, debug)
        if p:
            return p
    else:
        p = await _download_via_more_menu(page, "slides", stamp, debug)
        if p:
            return p

    log(f"  could not download {kind} automatically.")
    await dump_ui(page, f"{kind} download failed")
    await shot(page, f"nlm_08_{kind}_download_failed", True)
    return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run(args) -> int:
    _, cookies = load_cookies(args.profile)

    artifacts = []
    if args.audio:
        artifacts.append("audio")
    if args.slides:
        artifacts.append("slides")
    if not artifacts and not args.download_only:
        artifacts = ["audio"]  # default

    async with make_camoufox(args.headless) as context:
        page = await prepare_persistent_page(context, cookies)
        # Auto-dismiss any native dialog (e.g. a download-save error popup) so it
        # can't block the page.
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        if args.notebook:
            await goto_retry(page, args.notebook)
            await page.wait_for_timeout(6000)
            await shot(page, "nlm_01_notebook", args.debug)
            log(f"  opened existing notebook: {page.url}")
        else:
            if not await open_home(page, args.debug):
                if args.keep_open:
                    await hold(page)
                return 3
            if not await create_notebook(page, args.debug):
                if args.keep_open:
                    await hold(page)
                return 4
            await dismiss_dialogs(page)

            # Sources.
            ok_any = False
            if args.source_text:
                ok_any |= await add_source_text(page, args.source_text, args.debug)
            for url in args.source_url or []:
                ok_any |= await add_source_url(page, url, args.debug)
            for f in args.source_file or []:
                p = Path(f).expanduser()
                if not p.is_file():
                    log(f"  --source-file not found: {p}"); continue
                if p.suffix.lower() in (".txt", ".md", ".markdown", ".csv"):
                    ok_any |= await add_source_text(page, p.read_text(encoding="utf-8", errors="replace"), args.debug)
                else:
                    ok_any |= await add_source_file(page, p, args.debug)
            if args.discover:
                ok_any |= await add_source_discover(page, args.discover, args.debug)
            if not args.source_text and not args.source_url and not args.source_file and not args.discover:
                ok_any |= await add_source_text(page, SAMPLE_TEXT, args.debug)  # default sample
            if not ok_any:
                log("ERROR: no source was added.")
                if args.keep_open:
                    await hold(page)
                return 5
            await dismiss_dialogs(page)

        # Generate artifacts (skip if download-only).
        if not args.download_only:
            for kind in artifacts:
                instr = args.instructions if kind == "audio" else (args.slides_prompt or args.instructions)
                if not await generate_artifact(page, kind, args.language, instr,
                                               args.audio_format, args.audio_length, args.debug):
                    log(f"  {kind}: generation could not be started.")

        # Wait + download each.
        saved = []
        for kind in artifacts:
            if await wait_artifact_ready(page, kind, args.timeout, args.debug):
                p = await download_artifact(page, kind, args.debug)
                if p:
                    saved.append(p)

        if saved:
            log(f"Done. Saved: {', '.join(str(p) for p in saved)}")
        else:
            log("No artifacts downloaded. See debug/nlm_*.png.")
        await shot(page, "nlm_09_final", args.debug)
        if args.keep_open:
            await hold(page)
        return 0 if saved else 8


def parse_args(argv=None):
    default_title = f"Auto NB {time.strftime('%Y-%m-%d %H:%M')}"
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="default-release")
    p.add_argument("--notebook", default="", help="Operate on an existing notebook URL (skip create/source)")
    p.add_argument("--title", default=default_title)
    p.add_argument("--source-text", default="")
    p.add_argument("--source-url", action="append", help="Website/YouTube URL (repeatable)")
    p.add_argument("--source-file", action="append", help="File path to add as a source (repeatable)")
    p.add_argument("--discover", default="", help="Web-search query for NotebookLM to find & add sources")
    p.add_argument("--language", default="", help="Output language for artifacts, e.g. 'Russian'")
    p.add_argument("--audio", action="store_true", help="Generate an Audio Overview")
    p.add_argument("--slides", action="store_true", help="Generate a Slide Deck")
    p.add_argument("--instructions", default="", help="Focus/steering prompt for the Audio Overview")
    p.add_argument("--slides-prompt", default="", help="Description prompt for the Slide Deck")
    p.add_argument("--audio-format", default="", help="Deep Dive | Brief | Critique | Debate")
    p.add_argument("--audio-length", default="", help="Short | Default | Long")
    p.add_argument("--download-only", action="store_true", help="Skip generation; just download ready artifacts")
    p.add_argument("--timeout", type=int, default=600, help="Max seconds to wait per artifact (default 600)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
