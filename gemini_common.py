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
        humanize=True,
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


async def prepare_persistent_page(context, cookies: List[dict]):
    """Get a page from the persistent context, bootstrapping the Google session
    from Firefox cookies ONLY if the context isn't already logged in. After the
    first bootstrap, Camoufox keeps its own (rotating) session, so we never touch
    the Firefox cookie again — that is what stops logging the user out of Firefox.
    """
    page = context.pages[0] if context.pages else await context.new_page()
    if await context_logged_in(context):
        log("  reusing Camoufox's own persisted session (Firefox not touched)")
    else:
        await context.add_cookies(cookies)
        log("  bootstrapped session from Firefox cookies (first run / logged out)")
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
    url = page.url
    if "accounts.google.com" in url or "/ServiceLogin" in url:
        return False
    editor, _ = await find_editor(page)
    return editor is not None


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


async def send_message(page) -> bool:
    """Submit the current prompt. Enter sends in Gemini; the editor is focused
    after fill(), so press Enter on it (re-querying to be safe)."""
    editor, _ = await find_editor(page)
    if editor is None:
        return False
    try:
        await editor.press("Enter")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  send failed: {str(e).splitlines()[0]}")
        return False


async def hold(page) -> None:
    """Keep the browser open until interrupted (for --keep-open / debugging)."""
    import asyncio

    log("--keep-open: browser stays open. Press Ctrl+C to quit.")
    try:
        while True:
            await page.wait_for_timeout(1000)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
