#!/usr/bin/env python3
"""One-shot diagnostic: dump the composer's buttons and editor state so the
send fallback uses a selector that exists in the current Gemini build."""
import asyncio
import json

from gemini_common import (
    load_cookies, log, is_logged_in, make_camoufox, open_app,
    wait_for_editor, fill_prompt,
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
        editor, sel = await wait_for_editor(page)
        log(f"editor selector: {sel}")
        await fill_prompt(page, "hello diagnostic")
        await page.wait_for_timeout(2000)
        info = await page.evaluate(
            """() => {
                const out = {buttons: [], editor: null};
                for (const b of document.querySelectorAll('button')) {
                    const r = b.getBoundingClientRect();
                    if (r.bottom < innerHeight * 0.6) continue;  // composer area only
                    const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
                    out.buttons.push({
                        label: b.getAttribute('aria-label'),
                        mat: b.getAttribute('mattooltip'),
                        testid: b.getAttribute('data-test-id'),
                        disabled,
                        html: b.outerHTML.slice(0, 140),
                    });
                }
                const ed = document.querySelector('.ql-editor, rich-textarea div[contenteditable]');
                if (ed) out.editor = ed.className.slice(0, 80);
                return out;
            }"""
        )
        print(json.dumps(info, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
