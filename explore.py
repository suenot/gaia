#!/usr/bin/env python3
"""Throwaway UI explorer: deep-walk the DOM (including shadow roots) and dump
every clickable/labeled element near the Gemini composer, then click the '+' and
dump again. Reveals the real selectors for the video flow."""

import asyncio
from camoufox.async_api import AsyncCamoufox
from gemini_common import load_cookies, log, shot, open_app, is_logged_in, wait_for_editor

JS_DEEP_DUMP = r"""
() => {
  const out = [];
  const seen = new Set();
  function walk(root) {
    const els = root.querySelectorAll('*');
    for (const el of els) {
      if (seen.has(el)) continue;
      seen.add(el);
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute && el.getAttribute('role');
      const aria = el.getAttribute && el.getAttribute('aria-label');
      const type = el.getAttribute && el.getAttribute('type');
      const interesting = tag === 'button' || tag === 'input' || tag === 'mat-icon'
        || role === 'button' || aria || (el.getAttribute && el.getAttribute('mattooltip'));
      if (interesting) {
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {x:0,y:0,width:0,height:0};
        out.push({
          tag, role: role || '', aria: (aria||'').slice(0,50), type: type || '',
          text: (el.innerText || el.textContent || '').trim().slice(0,40),
          tip: (el.getAttribute && (el.getAttribute('mattooltip')||el.getAttribute('data-test-id'))) || '',
          x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
        });
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  return out;
}
"""


async def deep_dump(page, tag):
    items = await page.evaluate(JS_DEEP_DUMP)
    # Focus on visible, on-screen elements (skip zero-size / offscreen)
    vis = [i for i in items if i["w"] > 0 and i["h"] > 0]
    log(f"=== deep dump [{tag}] : {len(items)} interesting, {len(vis)} visible ===")
    for i in vis:
        log(f"  <{i['tag']}> role='{i['role']}' type='{i['type']}' aria='{i['aria']}' "
            f"text='{i['text']}' tip='{i['tip']}' @({i['x']},{i['y']} {i['w']}x{i['h']})")


async def main():
    _, cookies = load_cookies("default-release")
    async with AsyncCamoufox(headless=False, humanize=True, os="macos", geoip=True, block_images=False) as browser:
        page = await open_app(browser, cookies, debug=True)
        if not await is_logged_in(page):
            log("not logged in"); return
        await wait_for_editor(page)
        await deep_dump(page, "fresh chat")
        log(f"input[type=file] (playwright): {await page.locator('input[type=file]').count()}")

        # Click the composer's "Upload & tools" (+) button.
        btn = page.locator("button[aria-label='Upload & tools']")
        log(f"Upload & tools button count: {await btn.count()}")
        if await btn.count() > 0:
            await btn.first.click()
            await page.wait_for_timeout(1500)
            await shot(page, "ex_plus_menu", True)
            await deep_dump(page, "after Upload & tools")
            log(f"input[type=file] after menu: {await page.locator('input[type=file]').count()}")

        await page.wait_for_timeout(1500)


if __name__ == "__main__":
    asyncio.run(main())
