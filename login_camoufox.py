#!/usr/bin/env python3
"""
One-time setup for the STABLE, persistent Camoufox session used by all the other
scripts. Run this ONCE; afterwards every script reuses the same profile
(.camoufox_profile/) and the same pinned fingerprint (.camoufox_fp.pkl), so the
Google account always sees ONE device and your Firefox session is never disturbed.

Two ways to establish the session:

  * Manual login (recommended — fully decoupled from Firefox):
        python3 login_camoufox.py
    A Camoufox window opens on Gemini. If it's not logged in, sign into Google
    in THAT window. The session is saved into the persistent profile.

  * Bootstrap from your Firefox cookies (one-time copy of the current session):
        python3 login_camoufox.py --bootstrap
    Injects the Google cookies from your Firefox profile into the persistent
    Camoufox profile. (This shares the live cookie once; manual login avoids even
    that.)

After this, run gemini_image_gen.py / gemini_video_gen.py / flow_gen.py / etc. as
usual — they'll pick up the persistent session automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from gemini_common import (
    make_camoufox, prepare_persistent_page, load_cookies, navigate,
    is_logged_in, context_logged_in, log, hold, GEMINI_URL,
)


async def run(args) -> int:
    cookies = []
    if args.bootstrap:
        profile_path, cookies = load_cookies(args.profile)
        log(f"Bootstrap mode: {len(cookies)} cookies from {profile_path.name}")

    async with make_camoufox(headless=False) as context:
        already = await context_logged_in(context)
        log(f"Persistent profile already logged in: {already}")
        if args.bootstrap and not already and cookies:
            page = await prepare_persistent_page(context, cookies)
        else:
            page = context.pages[0] if context.pages else await context.new_page()

        await navigate(page, GEMINI_URL, debug=True, shot_name="login_01")

        if await is_logged_in(page):
            log("Logged in ✓ — the persistent Camoufox session is ready.")
            log("You can now run the other scripts; they'll reuse this profile.")
            if args.keep_open:
                await hold(page)
            return 0

        log("NOT logged in yet.")
        log(">>> Sign into Google in THIS Camoufox window now. <<<")
        log(f"Waiting up to {args.wait}s for login to complete...")
        deadline = time.time() + args.wait
        while time.time() < deadline:
            await page.wait_for_timeout(3000)
            if await is_logged_in(page):
                log("Login detected ✓ — saved to the persistent profile.")
                if args.keep_open:
                    await hold(page)
                return 0
        log("Timed out waiting for login. Re-run and finish signing in.")
        if args.keep_open:
            await hold(page)
        return 3


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bootstrap", action="store_true",
                   help="Inject Firefox cookies instead of manual login")
    p.add_argument("--profile", default="default-release",
                   help="Firefox profile to bootstrap from (with --bootstrap)")
    p.add_argument("--wait", type=int, default=300, help="Seconds to wait for manual login")
    p.add_argument("--keep-open", action="store_true", help="Keep the window open at the end")
    return p.parse_args(argv)


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
