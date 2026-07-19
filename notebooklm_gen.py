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
import re
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


async def robust_click(locator, timeout_ms: int = 4000) -> bool:
    """Click a locator, falling back to a JS click when a cdk-overlay-backdrop
    intercepts pointer events (recurring on the 2026 Gemini Notebook UI)."""
    try:
        await locator.click(timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        try:
            await locator.evaluate("el => el.click()")
            return True
        except Exception:  # noqa: BLE001
            return False


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
                if await robust_click(el, 3000):
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
# NotebookLM was rebranded to "Gemini Notebook" (2026-07): a one-time modal
# ("Let's go") blocks the page and button labels may say "notebook" with either
# branding. Keep both generations of selectors.
CREATE_BTN_SEL = ("button[aria-label='Create new notebook'], "
                  "button[aria-label='New notebook'], "
                  "button[aria-label='Create notebook'], "
                  "button[aria-label='Create new'], "
                  "button.create-new-button, "
                  # 2026-07 Gemini Notebook home: create action is a card tile,
                  # not a <button> — match anything clickable with the label text
                  "[role='button']:has-text('Create new notebook'), "
                  "mat-card:has-text('Create new notebook'), "
                  "div.create-notebook-card")


async def _dismiss_rebrand_dialog(page):
    for label in ("Let's go", "Got it", "Continue", "OK"):
        try:
            btn = page.locator(f"button:has-text(\"{label}\")")
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click(timeout=3000)
                log(f"  dismissed dialog via '{label}'")
                await page.wait_for_timeout(1000)
                return
        except Exception:  # noqa: BLE001
            continue


async def open_home(page, debug: bool) -> bool:
    log(f"Navigating to {HOME}")
    await goto_retry(page, HOME)
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.wait_for_selector(CREATE_BTN_SEL, state="visible", timeout=30_000)
    except Exception:  # noqa: BLE001
        pass
    await _dismiss_rebrand_dialog(page)
    await page.wait_for_timeout(2000)
    await shot(page, "nlm_01_home", debug)
    if "accounts.google.com" in page.url or "signin" in page.url:
        log(f"ERROR: sign-in wall: {page.url}")
        return False
    if await page.locator(CREATE_BTN_SEL).count() == 0:
        log("WARNING: 'Create new notebook' not found — may not be logged in.")
        return False
    log("Logged in ✓ (NotebookLM home)")
    return True


async def _click_create_notebook(page) -> bool:
    """Click a *visible* 'Create new notebook' button (there can be duplicates)."""
    await _dismiss_rebrand_dialog(page)
    for sel in ("button[aria-label='Create new notebook']", "button:has-text('Create new')",
                "button[aria-label='Create notebook']", "button:has-text('Create notebook')",
                "button[aria-label='New notebook']", "button:has-text('New notebook')",
                "button.create-new-button",
                # 2026-07 Gemini Notebook home: create action is a card tile
                "[role='button']:has-text('Create new notebook')",
                "mat-card:has-text('Create new notebook')"):
        loc = page.locator(sel)
        for i in range(await loc.count()):
            b = loc.nth(i)
            try:
                if await b.is_visible():
                    await b.click(timeout=4000)
                    return True
            except Exception:  # noqa: BLE001
                # A transient cdk-overlay-backdrop sometimes intercepts pointer
                # events even though the button is visible. Fall back to a direct
                # JS click that bypasses actionability/overlay checks.
                try:
                    if await b.is_visible():
                        await b.evaluate("el => el.click()")
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
    await robust_click(page.locator("button:has-text('Copied text')").first)
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
    await robust_click(page.locator("button:has-text('Websites')").first)
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
    # 2026-07 Gemini Notebook "Fast Research": results land in a panel with an
    # explicit "Import" button that appears only once research completes. Wait
    # for that button specifically (research can take 1-3 min), then click it.
    log("  waiting for Fast Research to complete (Import button)...")
    deadline = time.time() + 240
    imported = False
    while time.time() < deadline:
        btn = page.get_by_role("button", name=re.compile(r"^Import$", re.I))
        try:
            for i in range(await btn.count()):
                b = btn.nth(i)
                if await b.is_visible():
                    await b.click(timeout=6000)
                    imported = True
                    break
        except Exception:  # noqa: BLE001
            pass
        if imported:
            break
        await page.wait_for_timeout(3000)
    await dismiss_dialogs(page)
    await shot(page, "nlm_03_discover_results", debug)
    if imported:
        log("  discovered sources imported ✓")
        await page.wait_for_timeout(10000)
    else:
        # Fallback: try the older label variants, then don't hard-fail —
        # the primary source (URL/file) is enough to generate artifacts.
        if not await click_text(page, ["Add sources", "Add", "Insert"]):
            log("  WARNING: no Import button for discovered sources; "
                "continuing with primary source only.")
    # Any existing source row (incl. the primary URL/file) means we can proceed.
    if await _wait_source(page, debug, timeout_s=60):
        return True
    return imported


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
        # Gemini Notebook (2026-07 rebrand): Studio artifacts are tiles with
        # aria-label 'Audio Overview' / 'Slide Deck'; clicking the tile opens
        # the customize popover directly.
        tile = "Audio Overview" if kind == "audio" else "Slide Deck"
        btn = page.locator(f"[role='button'][aria-label='{tile}'], button[aria-label='{tile}']")
    if await btn.count() == 0:
        log(f"ERROR: '{label}' button not found in Studio.");
        await dump_ui(page, f"no {label}")
        return False
    await robust_click(btn.first)
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
    await robust_click(gen.last)
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
            ready = await page.locator(PLAY_SEL).count() > 0
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
    play = page.locator(PLAY_SEL)
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


# The audio play control's aria-label varies across NotebookLM UI revisions
# ("Play" on the Studio card, "Play audio" on the expanded player).
PLAY_SEL = "button[aria-label='Play'], button[aria-label='Play audio']"


async def _artifact_more_button(page, kind: str):
    """Return the More(⋮) button of the wanted *artifact card* in the Studio panel.

    Audio: the More right after a Play control. Slides/other: a Studio-side More
    that is NOT on the same row as ANY Play button (every audio card has its own
    Play, so excluding all Play rows leaves the non-audio artifacts)."""
    if kind == "audio":
        # The expanded audio player exposes its own overflow button whose
        # aria-label is "See more options for audio player" (not "More").
        for lbl in ("See more options for audio player",
                    "More options for Audio Overview", "More"):
            m = page.locator(f"button[aria-label='{lbl}']")
            try:
                if await m.count() > 0 and await m.first.is_visible():
                    return m.first
            except Exception:  # noqa: BLE001
                continue
        play = page.locator(PLAY_SEL)
        if await play.count() > 0:
            more = play.first.locator(
                "xpath=following::button[@aria-label='More' or "
                "@aria-label='See more options for audio player'][1]")
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
        play = page.locator(PLAY_SEL)
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
                     if kind == "slides"
                     else ["Download audio", "Download .m4a", "Download m4a",
                           "Download"])
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
        # Downloading audio first opens/expands the audio player, whose card then
        # gets mistaken for the slide card's More menu (slides download grabs the
        # audio file). Close the player first so the layout resets.
        for lbl in ("Close audio player", "Close"):
            try:
                c = page.locator(f"button[aria-label='{lbl}']")
                if await c.count() > 0 and await c.first.is_visible():
                    await c.first.click(timeout=2500)
                    await page.wait_for_timeout(800)
                    break
            except Exception:  # noqa: BLE001
                continue
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

        checks_ok = run_slide_checks(saved, args)

        await shot(page, "nlm_09_final", args.debug)
        if args.keep_open:
            await hold(page)
        if not saved:
            return 8
        return 0 if checks_ok else 9


# Cyrillic letters that exist in Ukrainian but NOT in Russian. NotebookLM
# occasionally slips one of these into a "Russian" slide deck.
_UA_ONLY = "іїєґ"
_UA_ONLY += _UA_ONLY.upper()


# Cyrillic letters whose glyphs are near-identical to Latin ones (OCR of Latin
# text frequently yields these when rus/ukr models are loaded).
_LATIN_TWINS = set("аеорсухкіїєтгпнимвдзбАЕОРСУХКІЇЄТГПНИМВДЗБ")


def _latin_misread(part):
    """True if `part` is likely an OCR mis-read of a Latin word: nearly every
    Cyrillic letter in it has a Latin glyph twin (Ргодисіїоп <- Production)."""
    letters = [c for c in part if c.isalpha()]
    if len(letters) < 3:
        return False
    cyr = [c for c in letters if "Ѐ" <= c <= "ӿ"]
    if not cyr:
        return False
    twins = [c for c in cyr if c in _LATIN_TWINS]
    return len(twins) >= 0.85 * len(cyr)


def check_ukrainian_in_pdf(pdf_path):
    """Scan a slide-deck PDF for Ukrainian-only Cyrillic characters. NotebookLM
    slides carry no text layer (they are rendered images), so this OCRs each page
    with tesseract (rus+ukr) and scans the result. Returns the list of offending
    words (empty if clean). Requires `pdftoppm` + `tesseract` with rus & ukr."""
    import subprocess, tempfile, glob, os
    bad = []
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(["pdftoppm", "-png", "-r", "200", str(pdf_path),
                            os.path.join(td, "pg")], check=True, capture_output=True)
        except Exception as e:  # noqa: BLE001
            log(f"  (Ukrainian check skipped — pdftoppm failed: {e})")
            return []
        for img in sorted(glob.glob(os.path.join(td, "pg*.png"))):
            try:
                # eng too, so Latin text (INPUT, ACTION, PPO) stays Latin instead
                # of being mis-read as Cyrillic look-alikes.
                out = subprocess.run(
                    ["tesseract", img, "-", "-l", "eng+rus+ukr"],
                    capture_output=True, text=True, check=True).stdout
            except Exception:  # noqa: BLE001
                continue
            for raw in out.split():
                word = raw.strip(".,:;()[]{}«»\"'—-–")
                if not any(ch in _UA_ONLY for ch in word):
                    continue
                cyr = [c for c in word if "Ѐ" <= c <= "ӿ"]
                # A genuine Russian/Ukrainian word: mostly Cyrillic, has a
                # lowercase letter (skip ALL-CAPS headers, which OCR mangles).
                # The per-slide "NotebookLM" watermark OCRs as Cyrillic garbage
                # like "Моїероок"/"ріероок" — not real Ukrainian text.
                if "ероок" in word.lower() or "оок" in word.lower()[-4:]:
                    continue
                # Latin words on slides (Production, limit, market...) OCR as
                # Cyrillic look-alikes with і/ї. If a hyphen-part containing the
                # Ukrainian letter is almost fully mappable to Latin glyph
                # twins, it is a mis-read Latin word, not Ukrainian text.
                if any(_latin_misread(part) for part in word.split("-")
                       if any(ch in _UA_ONLY for ch in part)):
                    log(f"  (RU slide check: skipping likely Latin mis-read: {word})")
                    continue
                if (len(word) >= 4 and len(cyr) >= 3
                        and len(cyr) >= 0.6 * len(word)
                        and any(c.islower() for c in cyr)):
                    bad.append(word)
    return sorted({w for w in bad if w})


def check_qr_in_pdf(pdf_path):
    """Render each PDF page and detect QR codes. Returns the list of 1-based page
    numbers that contain a QR code (empty if none). NotebookLM invents fake/
    decorative QR codes (usually on the last slide) — those decks are unusable.
    Requires `pdftoppm` (poppler) and cv2."""
    import subprocess, tempfile, glob, os, re
    try:
        import cv2
    except Exception:  # noqa: BLE001
        log("  (QR check skipped — cv2 not available)")
        return []
    pages = []
    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(["pdftoppm", "-png", "-r", "150", str(pdf_path),
                            os.path.join(td, "pg")], check=True,
                           capture_output=True)
        except Exception as e:  # noqa: BLE001
            log(f"  (QR check skipped — pdftoppm failed: {e})")
            return []
        det = cv2.QRCodeDetector()
        for img_path in sorted(glob.glob(os.path.join(td, "pg*.png"))):
            img = cv2.imread(img_path)
            if img is None:
                continue
            # Require an actual DECODE (non-empty payload), not just a detected
            # finder pattern — dense diagrams/grids trip the loose detector.
            try:
                ok, decoded, _, _ = det.detectAndDecodeMulti(img)
                ok = bool(ok) and any((d or "").strip() for d in decoded)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                m = re.search(r"pg[-_]?(\d+)", os.path.basename(img_path))
                pages.append(int(m.group(1)) if m else len(pages) + 1)
    return sorted(set(pages))


def run_slide_checks(saved, args):
    """Run the optional slide-deck quality checks on any downloaded PDF. Returns
    True if all enabled checks passed (or none enabled), False if a check found a
    problem — the deck should be regenerated."""
    ok = True
    for p in saved:
        if str(p).lower().endswith(".pdf"):
            if args.check_ru_slides:
                bad = check_ukrainian_in_pdf(p)
                if bad:
                    ok = False
                    log(f"  SLIDE CHECK FAILED [Ukrainian]: {p.name} contains "
                        f"Ukrainian-only letters in: {', '.join(bad[:12])}")
                else:
                    log(f"  slide check [Ukrainian] OK: {p.name}")
            if args.check_qr:
                qr = check_qr_in_pdf(p)
                if qr:
                    ok = False
                    log(f"  SLIDE CHECK FAILED [QR]: {p.name} has QR code(s) on "
                        f"page(s) {qr} — regenerate (fake decorative QR)")
                else:
                    log(f"  slide check [QR] OK: {p.name}")
    return ok


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
    p.add_argument("--check-ru-slides", action="store_true",
                   help="Scan the slide-deck PDF for Ukrainian-only letters "
                        "(NotebookLM sometimes slips them into Russian decks); "
                        "exit 9 if found")
    p.add_argument("--check-qr", action="store_true",
                   help="Detect QR codes in the slide-deck PDF (NotebookLM "
                        "invents fake decorative ones); exit 9 if found")
    return p.parse_args(argv)


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
