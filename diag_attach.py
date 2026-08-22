#!/usr/bin/env python3
"""Diagnostic: attach tiny JPEG, watch chip for 60s, then try to send."""
import asyncio
import json

from gemini_common import (
    load_cookies, log, is_logged_in, make_camoufox, open_app,
    wait_for_editor, fill_prompt, send_message, attach_files,
    _composer_has_text,
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
        editor, _ = await wait_for_editor(page)
        if editor is None:
            return 4

        log("attaching tiny.jpg (85KB)")
        if not await attach_files(page, ["/tmp/tiny.jpg"]):
            log("attach failed")
            return 5
        for i in range(12):
            await page.wait_for_timeout(5000)
            state = await page.evaluate(
                """() => {
                    const chips = [...document.querySelectorAll(
                        '[data-test-id*="file" i], [class*="attachment" i], [class*="upload" i]')];
                    return chips.slice(0, 3).map(c => c.outerHTML.slice(0, 200));
                }"""
            )
            err = await page.locator("text=/something went wrong|try again/i").count()
            log(f"t+{(i+1)*5}s err_toast={err} chips={json.dumps(state)[:300]}")
            if err:
                log("UPLOAD ERROR CONFIRMED")
                return 6
        await fill_prompt(page, "reply with the single word OK")
        ok = await send_message(page)
        log(f"send with tiny attachment: {ok}")
        return 0 if ok else 7
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
