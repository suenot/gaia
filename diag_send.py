#!/usr/bin/env python3
"""Diagnostic: text-only submit test in the new composer + clickable dump."""
import asyncio
import json

from gemini_common import (
    load_cookies, log, is_logged_in, make_camoufox, open_app,
    wait_for_editor, fill_prompt, send_message, _composer_has_text,
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
            log("no editor")
            return 4

        await fill_prompt(page, "reply with the single word OK")
        await page.wait_for_timeout(1500)
        log(f"before send, composer has text: {await _composer_has_text(page)}")

        # Try plain Enter via send_message
        ok = await send_message(page)
        log(f"send_message (Enter): {ok}")
        if not ok:
            # dump every clickable thing near the composer
            dump = await page.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll(
                            'button, [role=button], [mattooltip], [aria-label]')) {
                        const r = el.getBoundingClientRect();
                        if (r.bottom < innerHeight * 0.7 || r.width === 0) continue;
                        out.push({
                            tag: el.tagName,
                            label: el.getAttribute('aria-label'),
                            mat: el.getAttribute('mattooltip'),
                            role: el.getAttribute('role'),
                            cls: (el.className || '').toString().slice(0, 60),
                        });
                    }
                    return out;
                }"""
            )
            print(json.dumps(dump, indent=1)[:4000])
            # try Ctrl+Enter
            await page.keyboard.press("Control+Enter")
            await page.wait_for_timeout(2000)
            log(f"after Ctrl+Enter, composer still has text: {await _composer_has_text(page)}")
            return 5
        log("TEXT-ONLY SEND WORKS - attachment is what blocks submit")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
