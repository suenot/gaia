#!/usr/bin/env python3
"""Probe where the Gemini Notebook app now lives.

notebooklm.google.com started serving a marketing landing page after the
NotebookLM -> Gemini Notebook rename, so notebooklm_gen.py's HOME no longer
lands on the app. This walks the candidate URLs with the persistent Camoufox
session and reports which one shows the notebook list / create control.
"""
import asyncio
import sys

from gemini_common import load_cookies, log, make_camoufox, prepare_persistent_page, shot

CANDIDATES = [
    "https://notebooklm.google.com",
    "https://notebooklm.google.com/app",
    "https://gemini.google.com/notebook",
    "https://gemini.google.com/notebooks",
]

PROBE_JS = r"""
() => {
  const out=[]; const seen=new Set();
  function walk(root){
    let els; try{els=root.querySelectorAll('*')}catch(e){return}
    for(const el of els){
      if(seen.has(el))continue; seen.add(el);
      const tag=el.tagName.toLowerCase();
      const aria=el.getAttribute&&el.getAttribute('aria-label');
      const txt=(el.innerText||el.textContent||'').trim();
      if((tag==='button'||tag==='a')&&(aria||txt)){
        const r=el.getBoundingClientRect();
        if(r.width>0&&r.height>0)
          out.push({tag,aria:(aria||'').slice(0,70),text:txt.slice(0,50),href:el.href||''});
      }
      if(el.shadowRoot)walk(el.shadowRoot);
    }
  }
  walk(document); return out;
}
"""


async def main() -> None:
    _, cookies = load_cookies("default-release")
    async with make_camoufox(headless=True) as context:
        page = await prepare_persistent_page(context, cookies)
        for url in CANDIDATES:
            log(f"=== {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:  # noqa: BLE001
                log(f"  goto failed: {str(e).splitlines()[0][:80]}")
                continue
            await page.wait_for_timeout(6000)
            log(f"  final URL: {page.url}")
            log(f"  title: {await page.title()}")
            await shot(page, "probe_" + url.split("//")[1].replace("/", "_"))
            try:
                items = await page.evaluate(PROBE_JS)
            except Exception as e:  # noqa: BLE001
                log(f"  dump failed: {e}")
                continue
            log(f"  {len(items)} clickable:")
            for i in items[:40]:
                log(f"    <{i['tag']}> aria='{i['aria']}' text='{i['text']}' href='{i['href'][:60]}'")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
