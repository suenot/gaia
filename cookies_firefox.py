"""
Extract cookies from a Firefox profile and convert them to Playwright format.

Firefox keeps cookies in `cookies.sqlite` (plus `-wal`/`-shm` journal files when
the browser is running). We copy those files to a temp dir so we can read them
even while Firefox is open, then read `moz_cookies`.

Notes about this particular Firefox build (observed on the target machine):
  * `expiry` is stored in **milliseconds** since epoch (13 digits), not the
    classic seconds. We auto-detect and normalize to seconds for Playwright.
  * `sameSite` uses a non-standard integer encoding (e.g. 256). Because every
    `*.google.com` host shares the same registrable domain, all cookies are
    "same-site" for the Gemini app, so mapping them all to "Lax" is safe and
    avoids Playwright's rule that `SameSite=None` requires `Secure=True`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import List, Dict, Any

FIREFOX_PROFILES_DIR = Path.home() / "Library/Application Support/Firefox/Profiles"


def resolve_profile_path(profile: str) -> Path:
    """Resolve a --profile value to an actual profile directory.

    Accepts:
      * a full path to a profile directory,
      * a profile *name* (the part after the random prefix, e.g. "default-release"
        or "Профиль 3"),
      * an exact directory name (e.g. "v6saql6j.default-release").
    """
    p = Path(profile).expanduser()
    if p.is_dir() and (p / "cookies.sqlite").exists():
        return p

    if not FIREFOX_PROFILES_DIR.is_dir():
        raise FileNotFoundError(f"Firefox profiles dir not found: {FIREFOX_PROFILES_DIR}")

    candidates = [d for d in FIREFOX_PROFILES_DIR.iterdir() if d.is_dir()]
    # exact dir name
    for d in candidates:
        if d.name == profile:
            return d
    # name after the first dot ("xxxx.default-release" -> "default-release")
    for d in candidates:
        if "." in d.name and d.name.split(".", 1)[1] == profile:
            return d
    raise FileNotFoundError(
        f"Could not resolve Firefox profile '{profile}'. "
        f"Available: " + ", ".join(sorted(d.name for d in candidates))
    )


def _copy_db(profile_path: Path) -> Path:
    """Copy cookies.sqlite (+ wal/shm) to a temp dir; return the temp db path."""
    src = profile_path / "cookies.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"No cookies.sqlite in profile: {profile_path}")
    tmp = Path(tempfile.mkdtemp(prefix="ff_cookies_"))
    for ext in ("", "-wal", "-shm"):
        s = Path(str(src) + ext)
        if s.exists():
            shutil.copy2(s, tmp / ("cookies.sqlite" + ext))
    return tmp / "cookies.sqlite"


def _normalize_expiry(expiry: Any) -> float:
    if not expiry or expiry <= 0:
        return -1.0  # session cookie
    e = float(expiry)
    # Seconds-since-epoch never realistically exceed ~1e11 (year ~5138).
    # This build stores ms (~1.8e12), so divide down to seconds.
    if e > 1e11:
        e = e / 1000.0
    return e


def extract_cookies(profile_path: Path, host_like: str = "%google%") -> List[Dict[str, Any]]:
    """Return Playwright-format cookies for hosts matching `host_like`."""
    db = _copy_db(profile_path)
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly "
            "FROM moz_cookies WHERE host LIKE ?",
            (host_like,),
        ).fetchall()
    finally:
        con.close()
        shutil.rmtree(db.parent, ignore_errors=True)

    cookies: List[Dict[str, Any]] = []
    for host, name, value, path, expiry, is_secure, is_http_only in rows:
        if not host or not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value if value is not None else "",
                "domain": host,
                "path": path or "/",
                "expires": _normalize_expiry(expiry),
                "httpOnly": bool(is_http_only),
                "secure": bool(is_secure),
                # All *.google.com are same-site for the Gemini app; Lax is the
                # safe, always-accepted choice (no Secure requirement).
                "sameSite": "Lax",
            }
        )
    return cookies


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "default-release"
    pp = resolve_profile_path(name)
    cs = extract_cookies(pp)
    print(f"Profile: {pp}")
    print(f"Extracted {len(cs)} google cookies")
    auth = [c for c in cs if c["name"] in ("SID", "__Secure-1PSID", "SAPISID", "HSID", "SSID")]
    for c in auth:
        print(f"  {c['name']:18} domain={c['domain']:18} secure={c['secure']} httpOnly={c['httpOnly']} expires={c['expires']}")
