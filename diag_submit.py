#!/usr/bin/env python3
"""Diagnostic: find working submit path in new-input-ui with attachment present."""
import asyncio
import json

from gemini_common import (
    load_cookies, log, is_logged_in, make_camoufox, open_app,
    wait_for_editor, fill_prompt, attach_files, _composer_has_text,
)


async def dump_clickables(page) -> list:
    return await page.evaluate(
        """() => {
            const out = [];
            const walk = (root) => {
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) walk(el.shadowRoot);
                }
                for (const el of root.querySelectorAll('button, [role=button], [mattooltip], [aria-label]')) {
                    const r = el.getBoundingClientRect();
                    if (!r.width || r.bottom < innerHeight * 0.7) continue;
                    out.push({
                        tag: el.tagName, label: el.getAttribute('aria-label'),
                        mat: el.getAttribute('mattooltip'),
                        cls: (el.className || '').toString().slice(0, 50),
                    });
                }
            };
            walk(document);
            return out;
        }"""
    )


async def try_send(page, label: str, action) -> bool:
    await action()
    await page.wait_for_timeout(2500)
    still = await _composer_has_text(page)
    log(f"{label}: submitted={not still}")
    return not still


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

        log("attaching tiny.jpg")
        if not await attach_files(page, ["/tmp/tiny.jpg"]):
            return 5
        await page.wait_for_timeout(8000)
        await fill_prompt(page, "reply with the single word OK")
        await page.wait_for_timeout(1500)

        log("--- clickables (shadow-pierced) ---")
        for c in await dump_clickables(page):
            log(json.dumps(c))
        log("--- end dump ---")

        async def a1():
            await editor.click()
            await page.wait_for_timeout(300)
            await page.keyboard.press("Enter")

        if await try_send(page, "click-editor+Enter", a1):
            return 0
        if await try_send(page, "Ctrl+Enter", lambda: page.keyboard.press("Control+Enter")):
            return 0
        if await try_send(page, "Meta+Enter", lambda: page.keyboard.press("Meta+Enter")):
            return 0
        log("all submit variants failed")
        await page.screenshot(path="debug/diag_submit_dead.png")
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
