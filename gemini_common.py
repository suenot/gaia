"""
Shared Camoufox + Gemini helpers used by both the image and video scripts:
cookie loading, launching a logged-in page, the prompt editor, and screenshots.

Auth model (see README): we don't load the raw Firefox profile into Camoufox.
We extract the Google cookies from the chosen Firefox profile's cookies.sqlite
and inject them into the Camoufox context — the Gemini session is cookie-based,
so this lands us in a fully logged-in app.
"""

from __future__ import annotations

import pickle
import time
from pathlib import Path
from typing import List, Optional, Tuple

from cookies_firefox import resolve_profile_path, extract_cookies

HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
DEBUG_DIR = HERE / "debug"

# Stable-identity files (see make_camoufox): a persistent Camoufox profile keeps
# its OWN Google session across runs, and a pinned fingerprint makes every run
# look like the SAME device. Both are critical to avoid Google logging the user
# out (a new random fingerprint per launch looks like a new device each time).
CAMOUFOX_PROFILE_DIR = HERE / ".camoufox_profile"
FINGERPRINT_FILE = HERE / ".camoufox_fp.pkl"

GEMINI_URL = "https://gemini.google.com/app"

# Candidate selectors for the prompt editor. Gemini's input has historically been
# a Quill contenteditable; the current UI also exposes a real <textarea
# placeholder="Ask Gemini">. We accept either and prefer whichever is visible.
EDITOR_SELECTORS = [
    "div.ql-editor[contenteditable='true']",
    "rich-textarea div[contenteditable='true']",
    "div[contenteditable='true'][role='textbox']",
    "textarea[placeholder]",
    "textarea",
]
EDITOR_ANY = ", ".join(EDITOR_SELECTORS)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Stable, persistent Camoufox launch (prevents Google logging the user out)
# --------------------------------------------------------------------------- #
def _stable_fingerprint():
    """Generate ONE Camoufox fingerprint and reuse it forever (cached on disk) so
    the account always sees the same device. Returns a browserforge Fingerprint or
    None (then the caller pins os/window instead).

    Note: the pickle file is a *self-generated local cache* (we create the
    fingerprint object and write it ourselves to a gitignored file in this repo) —
    it is not untrusted input. Anyone who could tamper with it already has local
    write access to the project. We still guard with try/except.
    """
    try:
        if FINGERPRINT_FILE.exists():
            return pickle.loads(FINGERPRINT_FILE.read_bytes())
    except Exception:  # noqa: BLE001
        pass
    try:
        from browserforge.fingerprints import FingerprintGenerator

        # MUST be a Firefox fingerprint — Camoufox rejects Chrome (the default).
        # Constrain the screen to a normal laptop size so the window isn't huge.
        kwargs = dict(browser="firefox", os="macos")
        try:
            from browserforge.fingerprints import Screen
            kwargs["screen"] = Screen(max_width=1512, max_height=982)
        except Exception:  # noqa: BLE001
            pass
        fp = FingerprintGenerator().generate(**kwargs)
        try:
            FINGERPRINT_FILE.write_bytes(pickle.dumps(fp))
        except Exception:  # noqa: BLE001
            pass
        return fp
    except Exception:  # noqa: BLE001
        return None


def _geoip_available() -> bool:
    """True if Camoufox can determine the public IP for geoip (needs network)."""
    try:
        from camoufox.ip import public_ip

        public_ip()
        return True
    except Exception:  # noqa: BLE001
        return False


def make_camoufox(headless: bool = False):
    """Return an AsyncCamoufox context manager configured for a STABLE identity:
    a persistent profile (its own Google session survives across runs) and a
    pinned fingerprint (same device every launch). Yields a *persistent
    BrowserContext* (not a Browser) — use it directly with prepare_persistent_page.
    """
    from camoufox.async_api import AsyncCamoufox

    CAMOUFOX_PROFILE_DIR.mkdir(exist_ok=True)
    opts = dict(
        headless=headless,
        # Camoufox 0.5.x treats bool as an int and serializes humanize=True as
        # `humanize:maxTime=true`; the browser expects a numeric value and then
        # stops servicing trusted input. Pass an explicit float instead.
        humanize=1.0,
        # geoip needs a public-IP lookup; skip it gracefully if that's
        # unreachable (offline / flaky network) so launch doesn't crash with
        # InvalidIP. Camoufox then uses default geo/timezone.
        geoip=_geoip_available(),
        block_images=False,
        persistent_context=True,
        user_data_dir=str(CAMOUFOX_PROFILE_DIR),
        # Fixed, normal-sized window (not the fingerprint's full screen).
        window=(1440, 900),
    )
    fp = _stable_fingerprint()
    if fp is not None:
        opts["fingerprint"] = fp
    else:
        opts["os"] = "macos"
    return AsyncCamoufox(**opts)


async def context_logged_in(context) -> bool:
    """True if the persistent context already holds a Google session cookie."""
    try:
        cks = await context.cookies("https://gemini.google.com")
        return any(c["name"] in ("__Secure-1PSID", "SID") for c in cks)
    except Exception:  # noqa: BLE001
        return False


async def prepare_persistent_page(context, cookies: List[dict], *, force_bootstrap: bool = False):
    """Get a page from the persistent context, bootstrapping the Google session
    from Firefox cookies ONLY if the context isn't already logged in. After the
    first bootstrap, Camoufox keeps its own (rotating) session, so we never touch
    the Firefox cookie again — that is what stops logging the user out of Firefox.
    """
    page = context.pages[0] if context.pages else await context.new_page()
    if await context_logged_in(context) and not force_bootstrap:
        log("  reusing Camoufox's own persisted session (Firefox not touched)")
    else:
        await context.add_cookies(cookies)
        log("  bootstrapped session from Firefox cookies")
    return page


def load_cookies(profile: str) -> Tuple[Path, List[dict]]:
    profile_path = resolve_profile_path(profile)
    cookies = extract_cookies(profile_path)
    return profile_path, cookies


async def shot(page, name: str, enabled: bool = True) -> None:
    if not enabled:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=False)
        log(f"  screenshot -> debug/{name}.png")
    except Exception as e:  # noqa: BLE001
        log(f"  (screenshot {name} failed: {e})")


async def find_editor(page, require_visible: bool = True):
    """Return (locator, selector) for the first matching, visible editor."""
    for sel in EDITOR_SELECTORS:
        loc = page.locator(sel)
        try:
            if await loc.count() == 0:
                continue
            first = loc.first
            if require_visible and not await first.is_visible():
                continue
            return first, sel
        except Exception:  # noqa: BLE001
            continue
    return None, None


async def is_logged_in(page) -> bool:
    """True only when the *signed-in* app is up.

    The signed-out gemini.google.com landing page also renders a prompt editor, so
    "editor exists" alone reports a false positive — the run then proceeds and every
    tool in the '+' menu is disabled, which surfaces much later as an unrelated click
    timeout. Require a positive signed-in signal and the absence of a Sign in CTA.
    """
    url = page.url
    if "accounts.google.com" in url or "/ServiceLogin" in url:
        return False
    editor, _ = await find_editor(page)
    if editor is None:
        return False
    try:
        signed_out = await page.evaluate(
            """() => {
                const t = (document.body.innerText || '').toLowerCase();
                const cta = /\\bsign in\\b|\\bвойти\\b|sign in to try tools/.test(t);
                const acct = document.querySelector(
                    "a[href*='SignOutOptions'], [aria-label*='Google Account' i], "
                    + "[aria-label*='Аккаунт Google' i]") !== null;
                return cta && !acct;
            }"""
        )
    except Exception:  # noqa: BLE001
        signed_out = False
    if signed_out:
        log("  page renders the signed-out landing (Sign in CTA, no account chip)")
        return False
    return True


async def navigate(page, url: str, debug: bool = False, shot_name: str = "01_loaded"):
    """Navigate with retry (Google fires redirects that abort the first load),
    settle, and screenshot. Returns the page."""
    log(f"Navigating to {url}")
    last_err = None
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  navigation attempt {attempt + 1} failed: {str(e).splitlines()[0][:60]}")
            await page.wait_for_timeout(1500)
    if last_err is not None:
        raise last_err
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(2500)
    await shot(page, shot_name, debug)
    return page


async def open_app(context, cookies: List[dict], debug: bool = False):
    """Open Gemini in the persistent context (bootstrapping the Google session from
    Firefox cookies only if needed), and return the page.

    `context` is the persistent BrowserContext yielded by make_camoufox().
    Does not assert login — the caller should check `is_logged_in(page)`.
    """
    page = await prepare_persistent_page(context, cookies)
    log(f"Navigating to {GEMINI_URL}")
    # Gemini sometimes fires a redirect that aborts the first navigation
    # (NS_ERROR_ABORT). Retry a couple of times before giving up.
    last_err = None
    for attempt in range(3):
        try:
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60_000)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  navigation attempt {attempt + 1} failed: {str(e).splitlines()[0]}")
            await page.wait_for_timeout(1500)
    if last_err is not None:
        raise last_err
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(2500)
    await shot(page, "01_loaded", debug)
    return page


async def wait_for_editor(page, timeout_ms: int = 30_000, settle_ms: int = 2000):
    """Wait for the prompt editor to be visible and the entry animation to settle."""
    try:
        await page.wait_for_selector(EDITOR_ANY, state="visible", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(settle_ms)
    return await find_editor(page)


async def fill_prompt(page, text: str, attempts: int = 4) -> bool:
    """Put a (possibly multi-line) prompt into the editor WITHOUT sending it.

    Uses Playwright `fill()`, which sets the value via the editable element
    directly — bypassing the entry-animation overlay that intercepts real mouse
    clicks, handling newlines natively (no premature send), and leaving the
    element focused. Re-queries the editor each attempt to survive the app
    re-rendering/detaching it during hydration.
    """
    for attempt in range(attempts):
        editor, sel = await find_editor(page)
        if editor is None:
            await page.wait_for_timeout(1000)
            continue
        try:
            await editor.fill(text, timeout=8000)
            await page.wait_for_timeout(400)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  fill attempt {attempt + 1} failed ({sel}): {str(e).splitlines()[0]}")
            await page.wait_for_timeout(1200)
    return False


async def _composer_has_text(page) -> bool:
    """True while the prompt editor still holds unsent text."""
    try:
        return bool(await page.evaluate(
            """() => {
                const ed = document.querySelector("div.ql-editor[contenteditable='true']")
                    || document.querySelector("rich-textarea div[contenteditable='true']");
                if (!ed) return false;
                return (ed.innerText || '').trim().length > 0;
            }"""
        ))
    except Exception:  # noqa: BLE001
        return False


async def send_message(page) -> bool:
    """Submit the current prompt. Enter sends in Gemini; the editor is focused
    after fill(), so press Enter on it (re-querying to be safe)."""
    editor, _ = await find_editor(page)
    if editor is None:
        return False
    try:
        await editor.press("Enter")
    except Exception as e:  # noqa: BLE001
        log(f"  send failed: {str(e).splitlines()[0]}")
    await page.wait_for_timeout(1200)
    # The composer clears when the message is accepted. If the text is still
    # there, the locator keypress was swallowed - retry as a global trusted
    # keypress on the focused editor (the path that works in Camoufox).
    if await _composer_has_text(page):
        try:
            await page.keyboard.press("Enter")
        except Exception as e:  # noqa: BLE001
            log(f"  keyboard send failed: {str(e).splitlines()[0]}")
            return False
        await page.wait_for_timeout(1500)
        if await _composer_has_text(page):
            log("  prompt still unsent after two Enters")
            return False
    return True


# --------------------------------------------------------------------------- #
# Attachments. Gemini has no persistent <input type=file>: the '+' menu item opens
# the native file chooser, which Playwright intercepts. Shared so the image script
# can attach reference images the same way the video script attaches its source.

UPLOAD_TOOLS_BTN = "button[aria-label='Upload & tools']"

ATTACHMENT_PREVIEW = (
    "img[src^='blob:'], img[src^='data:image'], "
    "[data-test-id*='file' i], [class*='attachment' i], [class*='thumbnail' i], "
    "[class*='uploaded' i], [aria-label*='Remove' i]"
)


async def open_upload_menu(page) -> bool:
    """Open the '+' (Upload & tools) menu. Returns False if it never opens."""
    btn = page.locator(UPLOAD_TOOLS_BTN)
    if await btn.count() == 0:
        log(f"  '{UPLOAD_TOOLS_BTN}' not found")
        return False
    # The composer keeps animating (mat-ripple), so Playwright's actionability check
    # can stall on this button for a full timeout. Short real click, then dispatch the
    # DOM click directly; retry once, since this menu gates uploads and tools.
    for attempt in (1, 2):
        try:
            await btn.first.click(timeout=6_000)
        except Exception as e:  # noqa: BLE001
            log(f"  upload-menu click stalled ({str(e).splitlines()[0][:50]}); JS click")
            try:
                # A bare el.click() does not open this menu - the composer
                # listens for pointer events. Dispatch the full sequence.
                await btn.first.evaluate("""el => {
                    const box = el.getBoundingClientRect();
                    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
                    for (const type of ['pointerover', 'pointerdown', 'mousedown',
                                        'pointerup', 'mouseup', 'click']) {
                      el.dispatchEvent(new MouseEvent(type, {bubbles: true,
                        cancelable: true, composed: true, clientX: cx, clientY: cy}));
                    }
                }""")
            except Exception as e2:  # noqa: BLE001
                log(f"  JS click failed too: {str(e2).splitlines()[0][:60]}")
                return False
        try:
            await page.wait_for_selector("[role=menu]", state="visible", timeout=6000)
        except Exception:  # noqa: BLE001
            if attempt == 2:
                log("  upload menu never opened")
                return False
            await page.wait_for_timeout(1200)
            continue
        await page.wait_for_timeout(400)
        return True
    return False


async def wait_attachment(page, expected: int = 1, timeout_s: int = 60) -> bool:
    """Wait until `expected` upload previews have finished appearing."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if await page.locator(ATTACHMENT_PREVIEW).count() >= expected:
                await page.wait_for_timeout(2000)  # let processing settle
                return True
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1000)
    log(f"  (fewer than {expected} attachment previews within timeout)")
    return False


async def attach_files(page, paths, debug: bool = False) -> bool:
    """Attach one or more files through '+' -> Upload files."""
    paths = [str(p) for p in paths]
    if not paths:
        return True
    if not await open_upload_menu(page):
        return False
    up = page.locator("button[aria-label*='Upload files' i]")
    if await up.count() == 0:
        up = page.locator("[role=menuitem]:has-text('Upload files')")
    if await up.count() == 0:
        log("  'Upload files' item not found")
        await page.keyboard.press("Escape")
        return False
    try:
        # Trusted mouse input can stall on this menu item (Camoufox humanize
        # pipeline); focus the item via JS and activate it with a trusted
        # Space keypress instead - Enter is ignored, Space opens the chooser.
        focused = await page.evaluate("""() => {
          const menu = document.querySelector('[role=menu]');
          if (!menu) return false;
          const item = [...menu.querySelectorAll('[role=menuitem], button, a')]
            .find(el => /upload files/i.test((el.getAttribute('aria-label')||el.innerText||'')));
          if (!item) return false;
          item.focus && item.focus();
          return document.activeElement === item;
        }""")
        if not focused:
            raise RuntimeError("Upload files item not focusable")
        async with page.expect_file_chooser(timeout=15_000) as fc_info:
            await page.keyboard.press(" ")
        chooser = await fc_info.value
        await chooser.set_files(paths)   # the chooser accepts the whole batch at once
        log(f"  {len(paths)} file(s) sent to file chooser")
    except Exception as e:  # noqa: BLE001
        log(f"  file chooser failed: {str(e).splitlines()[0]}")
        return False
    ok = await wait_attachment(page, expected=len(paths))
    await shot(page, "12_attached", debug)
    return ok


async def hold(page) -> None:
    """Keep the browser open until interrupted (for --keep-open / debugging)."""
    import asyncio

    log("--keep-open: browser stays open. Press Ctrl+C to quit.")
    try:
        while True:
            await page.wait_for_timeout(1000)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
