#!/usr/bin/env python3
"""Diagnostic: attach tiny.jpg while logging upload-related network traffic."""
import asyncio

from gemini_common import (
    load_cookies, log, is_logged_in, make_camoufox, open_app,
    wait_for_editor, attach_files,
)


async def run() -> int:
    try:
        _, cookies = load_cookies("default-release")
    except Exception:
        cookies = []
    async with make_camoufox(False) as context:
        page = await open_app(context, cookies)
        if not await is_logged_in(page):
            log("not logged in")
            return 3

        seen = []

        def on_request(req):
            url = req.url
            if any(k in url for k in ("upload", "push", "content", "resumable")):
                seen.append(f"REQ  {req.method} {url[:110]}")

        def on_response(resp):
            url = resp.url
            if any(k in url for k in ("upload", "push", "content", "resumable")):
                seen.append(f"RESP {resp.status} {url[:110]}")

        page.on("request", on_request)
        page.on("response", on_response)

        editor, _ = await wait_for_editor(page)
        if editor is None:
            return 4
        log("attaching tiny.jpg")
        if not await attach_files(page, ["/tmp/tiny.jpg"]):
            log("attach failed")
            return 5
        for i in range(6):
            await page.wait_for_timeout(5000)
            log(f"t+{(i+1)*5}s traffic so far: {len(seen)}")
        log("--- upload-related traffic ---")
        for line in seen[-40:]:
            log(line)
        if not seen:
            log("NO upload traffic at all - file chooser feed never started an upload")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
