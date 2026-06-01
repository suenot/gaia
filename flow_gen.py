#!/usr/bin/env python3
"""
Generate video clips in **Google Flow** (https://labs.google/fx/tools/flow), the
Veo-powered AI filmmaking tool, driven through Camoufox + Playwright with the
user's authorized Google (labs.google) session.

This account's Flow project opens the **"Omni" agent** (a conversational panel on
the right: "Hi <name> / What would you like to do?") over a media grid. Two ways
to make a clip:
  * agent  : type a "Create a video: ..." prompt into the agent chat and send;
             the agent renders Veo clips into the media grid.
  * classic: if a classic bottom prompt bar (Text/Frames-to-Video) is present,
             use it directly (more deterministic).
The script auto-detects; `--mode` forces one. Generated clips are downloaded from
their `*.usercontent.google.com` URL via Playwright's APIRequestContext.

Usage
-----
  python3 flow_gen.py --prompt "a red maple leaf spinning as it falls, cinematic"
  python3 flow_gen.py --prompt-file prompts/flow.txt --debug
  python3 flow_gen.py --prompt "x" --explore --no-send --keep-open   # inspect UI
  python3 flow_gen.py --image still.png --prompt "animate: slow push-in"  # image->video

Outputs: ./output/flow_<ts>.mp4 ; screenshots ./debug/flow_NN_*.png (with --debug)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from gemini_common import (
    OUTPUT_DIR, load_cookies, log, shot, hold,
    make_camoufox, prepare_persistent_page,
)

ROOT = "https://labs.google/fx/tools/flow"
# Your Flow project (URL or bare id). Pass --project, or set GAIA_FLOW_PROJECT.
DEFAULT_PROJECT = os.environ.get("GAIA_FLOW_PROJECT", "")

AGENT_EDITOR = "div[role='textbox'][contenteditable='true']"

JS_DEEP_DUMP = r"""
() => {
  const out = []; const seen = new Set();
  function walk(root){
    let els; try { els = root.querySelectorAll('*'); } catch(e){ return; }
    for (const el of els){
      if (seen.has(el)) continue; seen.add(el);
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute && el.getAttribute('role');
      const aria = el.getAttribute && el.getAttribute('aria-label');
      const ph = el.getAttribute && el.getAttribute('placeholder');
      const interesting = ['button','input','textarea','video','audio'].includes(tag)
        || role==='button' || role==='textbox' || aria || ph
        || el.getAttribute?.('contenteditable')==='true';
      if (interesting){
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {x:0,y:0,width:0,height:0};
        out.push({ tag, role: role||'', aria:(aria||'').slice(0,50), ph:(ph||'').slice(0,40),
          text:(el.innerText||el.textContent||'').trim().slice(0,45),
          x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height) });
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  return out.filter(i => i.w>0 && i.h>0);
}
"""

JS_VIDEOS = r"""
() => Array.from(document.querySelectorAll('video')).map(v => ({
  src: v.currentSrc || v.src || (v.querySelector('source') ? v.querySelector('source').src : '') || '',
  w: v.videoWidth||0, h: v.videoHeight||0, dur: v.duration||0,
})).filter(v => v.src)
"""

JS_BODY_TAIL = "() => (document.body.innerText || '').slice(-600)"


async def deep_dump(page, tag: str) -> List[dict]:
    try:
        items = await page.evaluate(JS_DEEP_DUMP)
    except Exception as e:  # noqa: BLE001
        log(f"  deep_dump failed: {e}")
        return []
    log(f"=== deep dump [{tag}] : {len(items)} visible ===")
    for i in items:
        log(f"  <{i['tag']}> role='{i['role']}' aria='{i['aria']}' ph='{i['ph']}' "
            f"text='{i['text']}' @({i['x']},{i['y']} {i['w']}x{i['h']})")
    return items


async def goto_retry(page, url: str, tries: int = 3) -> bool:
    last = None
    for a in range(tries):
        try:
            await page.goto(url, wait_until="commit", timeout=60_000)
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  goto attempt {a + 1} to {url[:50]} failed: {str(e).splitlines()[0][:60]}")
            await page.wait_for_timeout(1500)
    return last is None


async def wait_canvas(page, tries: int = 25) -> bool:
    for i in range(tries):
        await page.wait_for_timeout(2500)
        try:
            has_editor = await page.locator(AGENT_EDITOR).count() > 0
            has_btns = await page.locator("button").count() > 3
            if has_editor and has_btns:
                log(f"  Flow canvas ready (poll {i})")
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


async def dismiss_welcome(page) -> None:
    """Dismiss the 'Welcome to Google Flow' splash / any modal backdrop."""
    for _ in range(5):
        try:
            bk = page.locator("div[data-state='open'][aria-hidden='true']")
            if await bk.count() == 0:
                return
        except Exception:  # noqa: BLE001
            return
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            await page.mouse.click(60, 700)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(600)


async def find_classic_bar(page):
    """Look for a classic Flow generation prompt bar (textarea/contenteditable near
    the bottom paired with a generate control). Returns (locator, kind) or (None, None)."""
    candidates = [
        ("textarea[placeholder*='Generate' i]", "textarea"),
        ("textarea[placeholder*='video' i]", "textarea"),
        ("textarea[placeholder*='Type' i]", "textarea"),
        ("textarea", "textarea"),
    ]
    vh = await page.evaluate("() => window.innerHeight")
    for sel, kind in candidates:
        loc = page.locator(sel)
        try:
            n = await loc.count()
            for i in range(n):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                box = await el.bounding_box()
                if box and box["y"] > vh * 0.55:  # bottom half = generation bar
                    return el, kind
        except Exception:  # noqa: BLE001
            continue
    return None, None


async def start_new_session(page, debug: bool) -> None:
    """Start a clean Omni agent session so we don't act on a stale, leftover
    proposal (the project may carry a previous conversation with pending
    Approve/Reject buttons)."""
    btn = page.locator("button:has-text('New session')")
    try:
        if await btn.count() > 0 and await btn.first.is_visible():
            await btn.first.click(timeout=4000)
            await page.wait_for_timeout(1500)
            log("  started a new agent session")
            await shot(page, "flow_11_new_session", debug)
    except Exception as e:  # noqa: BLE001
        log(f"  (could not start new session: {str(e).splitlines()[0][:50]})")


async def click_approve(page) -> bool:
    """The Omni agent proposes a generation gated by an 'Approve' (check) button.
    Click it if present. Returns True if a click happened."""
    for sel in ("button:has-text('Approve')", "button:has-text('check')",
                "button[aria-label*='Approve' i]"):
        b = page.locator(sel)
        try:
            if await b.count() > 0 and await b.first.is_visible() and await b.first.is_enabled():
                await b.first.click(timeout=3000)
                log("  clicked 'Approve' on the agent's proposal")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def submit_agent(page, prompt: str, debug: bool) -> bool:
    """Type into the Omni agent chat and send."""
    editor = page.locator(AGENT_EDITOR).first
    if await page.locator(AGENT_EDITOR).count() == 0:
        log("  agent editor not found")
        return False
    await editor.click()
    await page.wait_for_timeout(400)
    await page.keyboard.type(prompt, delay=12)
    await page.wait_for_timeout(500)
    await shot(page, "flow_12_typed", debug)
    txt = ""
    try:
        txt = (await editor.inner_text()) or ""
    except Exception:  # noqa: BLE001
        pass
    send = page.locator("button:has-text('arrow_forward')")
    try:
        if await send.count() > 0 and await send.first.is_enabled():
            await send.first.click()
        else:
            await page.keyboard.press("Enter")
    except Exception:  # noqa: BLE001
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    # Verify the editor cleared (message sent).
    try:
        after = (await editor.inner_text()) or ""
    except Exception:  # noqa: BLE001
        after = ""
    sent = (txt.strip()[:20] not in after) if txt.strip() else True
    log(f"  agent prompt sent={sent}")
    return True


async def submit_classic(page, bar, prompt: str, debug: bool) -> bool:
    try:
        await bar.click(timeout=4000)
        await bar.fill(prompt, timeout=8000)
    except Exception:  # noqa: BLE001
        try:
            await bar.type(prompt, delay=10)
        except Exception as e:  # noqa: BLE001
            log(f"  classic fill failed: {str(e).splitlines()[0]}")
            return False
    await page.wait_for_timeout(500)
    await shot(page, "flow_12_typed", debug)
    # Generate button: arrow_forward / 'Generate' / 'Create'
    for sel in ("button:has-text('arrow_forward')", "button[aria-label*='Generate' i]",
                "button:has-text('Generate')", "button:has-text('Create')"):
        b = page.locator(sel)
        try:
            if await b.count() > 0 and await b.first.is_enabled():
                await b.first.click()
                log(f"  classic generate via {sel}")
                return True
        except Exception:  # noqa: BLE001
            continue
    await bar.press("Enter")
    return True


async def wait_for_video(page, timeout_s: int, quiet_s: float, debug: bool) -> List[dict]:
    deadline = time.time() + timeout_s
    seen: dict = {}
    last_change = None
    last_log = 0.0
    start = time.time()
    approvals = 0
    while time.time() < deadline:
        # The agent gates generation behind an 'Approve' button that may appear a
        # few seconds after the request — click it whenever it shows up.
        if approvals < 3 and not seen:
            if await click_approve(page):
                approvals += 1
                await shot(page, "flow_15_approved", debug)
        try:
            vids = await page.evaluate(JS_VIDEOS)
        except Exception:  # noqa: BLE001
            vids = []
        changed = False
        for v in vids:
            if v["src"] not in seen:
                seen[v["src"]] = v
                changed = True
                log(f"  new video: {v['w']}x{v['h']} dur={v['dur']:.1f}s {v['src'][:60]}...")
        if changed:
            last_change = time.time()
            await shot(page, "flow_20_video", debug)
        if seen and last_change is not None and (time.time() - last_change) >= quiet_s:
            break
        now = time.time()
        if now - last_log > 20:
            elapsed = int(now - start)
            try:
                tail = await page.evaluate(JS_BODY_TAIL)
            except Exception:  # noqa: BLE001
                tail = ""
            log(f"  ...waiting for clip ({elapsed}s); agent tail: {tail[-120:]!r}")
            last_log = now
            if debug and elapsed and elapsed % 60 < 2:
                await shot(page, f"flow_wait_{elapsed}s", True)
        await page.wait_for_timeout(2500)
    return list(seen.values())


async def download_video(page, video: dict) -> Optional[Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"flow_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    src = video["src"]
    if src.startswith("http"):
        try:
            resp = await page.context.request.get(src, timeout=120_000)
            if resp.ok:
                body = await resp.body()
                if len(body) > 10_000:
                    out.write_bytes(body)
                    log(f"  saved {out.name} via context.request ({len(body):,} bytes)")
                    return out
                log(f"  context.request returned only {len(body)} bytes")
            else:
                log(f"  context.request HTTP {resp.status}")
        except Exception as e:  # noqa: BLE001
            log(f"  context.request failed: {str(e).splitlines()[0][:70]}")
    # Fallback: UI download button -> browser download.
    try:
        async with page.expect_download(timeout=30_000) as dl_info:
            for sel in ("button[aria-label*='Download' i]", "button:has-text('download')",
                        "button:has-text('Download')"):
                b = page.locator(sel)
                if await b.count() > 0 and await b.first.is_visible():
                    await b.first.click()
                    break
        dl = await dl_info.value
        await dl.save_as(str(out))
        log(f"  saved {out.name} via UI download")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"  UI download failed: {str(e).splitlines()[0][:70]}")
    return None


async def run(args) -> int:
    image_path = None
    if args.image:
        image_path = Path(args.image).expanduser()
        if not image_path.is_file():
            log(f"ERROR: image not found: {image_path}")
            return 2

    profile_path, cookies = load_cookies(args.profile)
    project = args.project or DEFAULT_PROJECT
    if not project:
        log("ERROR: no Flow project. Pass --project <url-or-id> or set GAIA_FLOW_PROJECT.")
        return 2
    if not project.startswith("http"):
        project = f"{ROOT}/project/{project}"

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        log("ERROR: no prompt (use --prompt or --prompt-file)")
        return 2
    log(f"Firefox profile: {profile_path.name}; {len(cookies)} cookies")
    log(f"Project: {project}")
    log(f"Prompt: {prompt[:80]}...")

    async with make_camoufox(args.headless) as context:
        # Flow throws benign client errors during boot; swallow them.
        await context.add_init_script(
            "window.addEventListener('error',e=>e.preventDefault(),true);"
            "window.addEventListener('unhandledrejection',e=>e.preventDefault(),true);"
        )
        page = await prepare_persistent_page(context, cookies)
        page.on("pageerror", lambda e: None)
        try:
            await page.set_viewport_size({"width": 1680, "height": 1050})
        except Exception:  # noqa: BLE001
            pass

        await goto_retry(page, ROOT)
        await page.wait_for_timeout(2500)
        await goto_retry(page, project)
        if not await wait_canvas(page):
            log("ERROR: Flow canvas/agent did not load (sign-in wall or slow).")
            await shot(page, "flow_02_no_canvas", True)
            if "accounts.google" in page.url:
                log(f"  sign-in wall: {page.url}")
            if args.keep_open:
                await hold(page)
            return 3
        await page.wait_for_timeout(1500)
        await dismiss_welcome(page)
        await shot(page, "flow_10_ready", args.debug)
        log(f"  url: {page.url}")

        if args.explore:
            await deep_dump(page, "flow ready")

        # Choose generation path.
        bar, kind = (None, None)
        if args.mode in ("auto", "classic"):
            bar, kind = await find_classic_bar(page)
        use_classic = bar is not None and args.mode != "agent"
        log(f"  generation path: {'classic bar' if use_classic else 'omni agent'}")

        if args.explore and args.no_send:
            log("--explore --no-send: stopping before generation.")
            if args.keep_open:
                await hold(page)
            return 0

        # Start a clean agent session (avoid stale Approve/Reject proposals).
        if not use_classic and not args.keep_session:
            await start_new_session(page, args.debug)

        # For the agent path, phrase the request so Omni routes to Veo video.
        agent_prompt = prompt
        if not use_classic and not prompt.lower().lstrip().startswith(("create", "generate", "make")):
            agent_prompt = f"Create a video: {prompt}"

        if use_classic:
            ok = await submit_classic(page, bar, prompt, args.debug)
        else:
            ok = await submit_agent(page, agent_prompt, args.debug)
        if not ok:
            log("ERROR: could not submit the prompt.")
            await shot(page, "flow_13_submit_failed", True)
            if args.keep_open:
                await hold(page)
            return 4
        log("Submitted; waiting for the Veo clip (can take a few minutes)...")
        await shot(page, "flow_14_sent", args.debug)

        videos = await wait_for_video(page, timeout_s=args.timeout, quiet_s=args.quiet, debug=args.debug)
        await shot(page, "flow_30_result", args.debug)
        if not videos:
            log("No clip detected. Check debug/flow_30_result.png — the Omni agent may "
                "have replied with text/questions instead of generating, or hit a limit.")
            if args.explore:
                await deep_dump(page, "flow no video")
            if args.keep_open:
                await hold(page)
            return 5

        videos.sort(key=lambda v: (v.get("dur") or 0, v.get("w") or 0), reverse=True)
        log(f"Detected {len(videos)} clip(s); downloading the best...")
        saved = await download_video(page, videos[0])
        if saved:
            log(f"Done. Saved {saved}")
        else:
            log("Clip detected but download failed; use --keep-open to grab it manually.")
        if args.keep_open:
            await hold(page)
        return 0 if saved else 5


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompt", default="", help="Inline prompt")
    p.add_argument("--prompt-file", default="", help="Read prompt from file")
    p.add_argument("--project", default="", help="Flow project URL or bare id (or set GAIA_FLOW_PROJECT)")
    p.add_argument("--image", default="", help="Source image for image->video (optional, experimental)")
    p.add_argument("--mode", choices=["auto", "agent", "classic"], default="auto",
                   help="Generation path (default auto: classic bar if present, else Omni agent)")
    p.add_argument("--keep-session", action="store_true",
                   help="Do NOT start a new agent session (continue the project's current chat)")
    p.add_argument("--profile", default="default-release", help="Firefox profile logged into Google")
    p.add_argument("--timeout", type=int, default=600, help="Max seconds to wait for the clip (default 600)")
    p.add_argument("--quiet", type=float, default=10.0, help="Seconds of no change before done (default 10)")
    p.add_argument("--headless", action="store_true", help="Run headless (visible is more reliable)")
    p.add_argument("--keep-open", action="store_true", help="Keep the browser open after finishing")
    p.add_argument("--debug", action="store_true", help="Save step screenshots to ./debug/flow_NN_*.png")
    p.add_argument("--explore", action="store_true", help="Deep-dump the UI (pierces shadow DOM)")
    p.add_argument("--no-send", action="store_true", help="With --explore: stop before generating")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
