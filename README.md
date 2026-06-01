# GAIA — Google AI Automation

GAIA drives Google's creative AI tools under your **authorized Google session**,
through **Camoufox** (an anti-detect Firefox build) + Playwright:

- **Gemini** — text→image, image→video and text→video (Veo)
- **Flow** — Veo film clips inside a Flow project (Omni agent)
- **NotebookLM** — create notebooks, add/discover sources, generate & download
  Audio Overviews and Slide Decks (with output language & steering prompts)
- **Music** — Gemini "Create music" / Labs MusicFX *(experimental)*

It does *not* copy your Firefox profile into Camoufox. It reads the Google cookies
from Firefox's `cookies.sqlite` once to bootstrap a **persistent, stable Camoufox
profile** (`.camoufox_profile/`) with a **pinned fingerprint** — after that GAIA
keeps its own session and your Firefox login is never touched again.

## Setup

```bash
pip install -r requirements.txt
python3 -m camoufox fetch            # one-time: download the Camoufox browser

# One-time: establish the persistent session (bootstrap from Firefox cookies)
python3 login_camoufox.py --bootstrap
# (or `python3 login_camoufox.py` and sign into Google in the Camoufox window)
```

> **Single-session rule:** GAIA uses ONE Google account. Run only **one** Camoufox
> at a time — never several in parallel. Each launch with a different fingerprint
> looks like a new device; concurrent/many-fingerprint use makes Google invalidate
> the session (logging you out). The pinned fingerprint + persistent profile above
> prevent that.

## Files

| File | Purpose |
|------|---------|
| `gemini_image_gen.py` | Text → image: log in via cookies, send prompt, download image(s) — **verified** |
| `gemini_video_gen.py` | Image→video **and** text→video (Veo): fresh chat, optional image + prompt, download `.mp4` — **verified** |
| `flow_gen.py`         | Google **Flow** (Veo): drive the Omni agent in a project (prompt → Approve → clip), download `.mp4` — **verified** |
| `music_gen.py`        | Music: Gemini "Create music" / Labs MusicFX — *built, not yet verified end-to-end* |
| `notebooklm_gen.py`   | NotebookLM: create notebook, add source, Audio Overview — *create+source work; audio gen needs the Studio tab* |
| `gemini_common.py`    | Shared helpers: cookies, launch+login, prompt editor, screenshots |
| `cookies_firefox.py`  | Extract & convert Firefox cookies → Playwright format |
| `explore.py`          | Dev tool: deep-walk the DOM (incl. shadow roots) to find selectors if the UI changes |
| `prompts/*.txt`       | Example prompts (image, motion, text-video) |
| `output/`             | Saved images (`*.png`) and videos (`*.mp4`) |
| `debug/`              | Step screenshots (only with `--debug`) |

> **Single-session rule:** these scripts reuse ONE Google account. Run only **one**
> Camoufox at a time — never several in parallel. Each Camoufox has a different
> random fingerprint, so concurrent sessions look like the account being used from
> many new devices at once and Google will invalidate the session (logging you out
> of Firefox too).

## Requirements

```bash
pip install -r requirements.txt
python3 -m camoufox fetch    # one-time: download the Camoufox browser binary
```

Already satisfied on this machine (Camoufox 0.4.11, browser 135.0.1-beta.24).

## Usage

```bash
# Example prompt, default profile (default-release), visible window
python3 gemini_image_gen.py --prompt-file prompts/example.txt

# Inline prompt
python3 gemini_image_gen.py --prompt "a red fox in deep snow, cinematic, 16:9"

# Pick a different Firefox profile (name, dir name, or full path)
python3 gemini_image_gen.py --profile "Профиль 3" --prompt-file prompts/example.txt

# Debugging: save step screenshots and keep the window open at the end
python3 gemini_image_gen.py --prompt-file prompts/example.txt --debug --keep-open
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--profile`     | `default-release` | Firefox profile logged into Gemini (name / dir / path) |
| `--prompt`      | — | Inline prompt text |
| `--prompt-file` | — | Read the prompt from a file (use this for multi-line prompts) |
| `--headless`    | off | Run with no visible window (visible is more reliable with Google) |
| `--timeout`     | `240` | Max seconds to wait for image generation |
| `--quiet`       | `8` | Seconds of no new images before generation is considered finished |
| `--keep-open`   | off | Leave the browser open after finishing (Ctrl+C to quit) |
| `--debug`       | off | Save `debug/NN_*.png` screenshots at each step |

Exit codes: `0` ok · `2` no prompt · `3` not logged in · `4` editor/input problem ·
`5` no images produced.

## Video (image → video, or text → video)

Gemini makes a video in a **fresh chat**. `gemini_video_gen.py` supports both:
- **image → video**: attach a source image + a motion prompt (`--image`),
- **text → video**: prompt only (omit `--image`; make the prompt explicitly ask
  for a video, e.g. "Generate an 8-second cinematic video: …").

It downloads the resulting `.mp4`.

```bash
# image -> video: animate an image with a motion prompt
python3 gemini_video_gen.py --image output/20260531_201357_1.png --prompt-file prompts/motion.txt

# text -> video: prompt only
python3 gemini_video_gen.py --prompt-file prompts/video_text.txt --debug

# inline prompt
python3 gemini_video_gen.py --prompt "Generate a video: a red fox running through deep snow, slow-mo, 16:9"
```

### Video options

| Flag | Default | Meaning |
|------|---------|---------|
| `--image`       | *(none)* | Source image to animate (image→video). Omit for text→video |
| `--prompt` / `--prompt-file` | — | Motion/animation prompt |
| `--profile`     | `default-release` | Firefox profile logged into Gemini |
| `--timeout`     | `600` | Max seconds to wait for the video (Veo takes ~1–3 min) |
| `--quiet`       | `10` | Seconds of no change before the video is considered done |
| `--video-tool`  | off | Open the dedicated "Create video" *studio* instead (experimental; the default plain chat flow is what works) |
| `--headless` / `--keep-open` / `--debug` | off | Same as the image script |

Exit codes: as above, plus `6` could not attach the image. Output: a 720p, ~10s
H.264+AAC `.mp4` in `output/`.

> The composer's **"Create video"** menu item opens a *separate* full-screen video
> studio (templates, Gemini Omni) — that's **not** needed and is avoided by default.
> Just uploading the image + a motion prompt in a normal chat triggers Veo inline
> ("Your video is ready!"). `--video-tool` is only there if you want the studio.

## Flow (Veo clips in a Flow project)

`flow_gen.py` drives **Google Flow** (labs.google/fx/tools/flow). On this account a
project opens the **"Omni" agent** (a chat panel), and generation is **gated by an
"Approve" button**: you send a request, the agent proposes a clip, you Approve, and
Veo renders 1–2 clips into the media grid. The script does all of that and
downloads a clip.

```bash
python3 flow_gen.py --prompt "a red maple leaf spinning as it falls, cinematic, 16:9"
python3 flow_gen.py --prompt-file prompts/flow.txt --project <url-or-id> --debug
python3 flow_gen.py --prompt "x" --explore --no-send --keep-open   # inspect the UI
```

Key flags: `--project` (URL or bare id; or set GAIA_FLOW_PROJECT), `--mode`
(`auto`/`agent`/`classic`), `--keep-session` (don't start a fresh agent chat),
`--timeout`, `--debug`. Output: a 720p ~10s `.mp4` in `output/`. Verified: prompt →
Approve → clip downloaded from the `labs.google/fx/api/.../media…` URL via
`context.request`.

## How it works

1. **Cookies** — `cookies_firefox.py` copies `cookies.sqlite` (+ WAL journal) so it
   reads even while Firefox is open, pulls every `*.google.com` cookie, and
   normalizes them: this Firefox build stores `expiry` in **milliseconds** (auto
   converted to seconds), and all cookies are mapped to `SameSite=Lax` (every
   `*.google.com` host is same-site for the Gemini app, so Lax is always sent and
   avoids Playwright's `SameSite=None`-requires-`Secure` rule).
2. **Launch** — `AsyncCamoufox(os="macos", humanize=True, geoip=True)`; cookies are
   added to a fresh context before navigating to `gemini.google.com/app`.
3. **Prompt** — the Quill `contenteditable` editor is filled via Playwright
   `fill()` (bypasses the entry-animation overlay that intercepts real clicks and
   handles multi-line without an early send), then `Enter` submits.
4. **Capture (image)** — polls the DOM for new large images from Google's CDN /
   blob URLs until they stop changing. Gemini serves generated images as `blob:`
   URLs that it **revokes** right after the `<img>` loads, so they're extracted
   from the already-decoded `<img>` via a **canvas → PNG** dump at full native
   resolution (with an element-screenshot fallback for any CORS-tainted image).
5. **Video flow** — the composer loads as a collapsed "Ask Gemini" splash, so the
   script focuses the editor to expand the full toolbar, opens **"Upload & tools"
   → "Upload files"** (a native file chooser, intercepted with
   `expect_file_chooser`), fills the motion prompt, and **clicks Send, verifying
   the composer actually cleared** (with an attachment the Send button stays
   disabled until the upload finishes). It then polls for the result `<video>` and
   downloads its `*.usercontent.google.com` URL via Playwright's
   **APIRequestContext** (`context.request.get`), which carries the session
   cookies and bypasses page CORS.

## Notes & limitations

- Gemini's UI selectors can change; if the input isn't found, run with `--debug`
  and inspect `debug/03_before_type.png` (image) or `debug/13_ready_to_send.png`
  (video). `explore.py` deep-dumps the live DOM (including shadow roots) to find
  new selectors.
- The UI on this account renders in **English** (aria-labels like "Upload & tools",
  "Send message", "Create video"), even though the OS/profile is Russian.
- Video generation needs Veo access on the account; "No video detected" usually
  means a quota/limit or a changed UI — check `debug/20_result.png`.
- "No text" in a prompt is a *request* to the model — Gemini may still render
  labels. That's model behavior, not a script bug.
- If you see "not logged in", the chosen profile's Google session may be expired
  or it's a different account — pick another `--profile`.
- Keep the session/cookies private; they grant access to the Google account.
