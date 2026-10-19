# GAIA — Google AI Automation

<p align="center">
  <img src="assets/gaia_comic.png" alt="GAIA — automate Google's creative AI from one logged-in session" width="820">
</p>

GAIA drives Google's creative AI — **Gemini, Flow, NotebookLM** — to generate
**text, images, video, music, slides and podcasts**, with **no API keys**: it just
uses your **logged-in Firefox/Google session**, through **Camoufox** (an anti-detect
Firefox build) + Playwright. (The one exception is the AI Studio speech tools below:
they call a documented API with an API key you create yourself.)

- **Gemini** — text→image, image→video and text→video (Veo)
- **Flow** — Veo film clips in a Flow project (Nano Banana edits, Veo 3, Omni agent)
- **NotebookLM** — create notebooks, add/discover sources, generate & download
  **Audio Overviews (podcasts)** and **Slide Decks** in any language, with steering prompts
- **Music** — Gemini "Create music" / Labs MusicFX *(experimental)*
- **AI Studio speech** — speech-to-text, text-to-speech and replicating **your own**
  voice through the documented Gemini API. This is the one part that uses an API key
  and not the browser session — see [AI Studio speech](#ai-studio-speech-stt--tts--voice-replication)

## 🏭 The Content Factory

GAIA is **stage 1** of an automated pipeline that turns a **blog article into a
published YouTube video** — no API keys, driven end-to-end through logged-in
browser sessions (Camoufox) and local media tooling.

| # | Stage | Repo | What it does |
|---|-------|------|--------------|
| **1** | **Generate** | **[gaia](https://github.com/suenot/gaia)** ⬅ *this repo* | Drive NotebookLM / Gemini / Flow from a logged-in session → audio overview + slide deck |
| 2 | Build | [video-maker](https://github.com/suenot/video-maker) | Audio narration + slide-deck PDF → synced MP4 (+ SRT, thumbnail) |
| 3 | Describe | [video-metadata](https://github.com/suenot/video-metadata) | Video + article → YouTube title / description / tags / chapter timestamps |
| 4 | Publish | [video-publisher](https://github.com/suenot/video-publisher) | Drive YouTube Studio → upload with metadata, channel switch, visibility |

**Flow:** `article → gaia → video-maker → video-metadata → video-publisher → YouTube`
(the published video is then embedded back into the blog article).

It does *not* copy your Firefox profile into Camoufox. It reads the Google cookies
from Firefox's `cookies.sqlite` once to bootstrap a **persistent, stable Camoufox
profile** (`.camoufox_profile/`) with a **pinned fingerprint** — after that GAIA
keeps its own session and your Firefox login is never touched again.

## ⚠️ Disclaimer

> [!CAUTION]
> **For personal and educational use only.** GAIA automates **your own**
> authenticated Google session. It is **NOT affiliated with, authorized by, or
> endorsed by Google**, and automating Google products may violate their Terms of
> Service — **use it at your own risk**: it can get your Google account
> rate-limited or **suspended**. Use only your own account, **one session at a
> time**; do **NOT** run it as a hosted service, a bot farm, or against accounts
> you do not own. Provided **AS IS, without warranty of any kind**.
>
> 🇷🇺 Только для личного и образовательного использования: вы автоматизируете
> собственную сессию Google и действуете на свой страх и риск (возможны
> ограничения или блокировка аккаунта).

## Requirements

> [!IMPORTANT]
> **Use Python 3.11, 3.12, or 3.13 — not 3.14.** The pinned Camoufox 0.5.4
> package uses Playwright 1.59–1.60, matching the current Camoufox browser
> protocol. Create the venv with a supported interpreter, e.g.
> `python3.11 -m venv venv`.

## Setup

```bash
python3.11 -m venv venv && source venv/bin/activate   # Python 3.11–3.13, NOT 3.14
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
| `login_camoufox.py`   | **One-time setup**: bootstrap the persistent Camoufox session (from Firefox cookies or a manual sign-in) |
| `gemini_image_gen.py` | Text → image: send prompt, download image(s) — **verified** |
| `gemini_video_gen.py` | Image→video **and** text→video (Veo): optional image + prompt, download `.mp4` — **verified** |
| `flow_gen.py`         | Google **Flow** (Veo): drive the Omni agent in a project (prompt → Approve → clip), download `.mp4` — **verified** |
| `notebooklm_gen.py`   | NotebookLM: create notebook, add/discover sources, generate & download **Audio Overview + Slide Deck** in any language — **verified** |
| `youtube_upload.py`   | YouTube Studio: upload a video with title/description/tags/thumbnail, audience + visibility, from a `video_maker` metadata JSON — no Data API |
| `music_gen.py`        | Music: Gemini "Create music" / Labs MusicFX — *built, not yet verified end-to-end* |
| `aistudio_speech.py`  | AI Studio speech CLI: `check`, `voices list/replicate/delete`, `consent-text`, `tts`, `stt` — Gemini API + your own key, **not** the Camoufox session (package `aistudio/`, `requirements-aistudio.txt`, tests in `tests/aistudio/`) |
| `gemini_common.py`    | Shared helpers: persistent launch, cookies, prompt editor, screenshots |
| `cookies_firefox.py`  | Extract & convert Firefox cookies → Playwright format |
| `explore.py`          | Dev tool: deep-walk the DOM (incl. shadow roots) to find selectors if the UI changes |
| `prompts/*.txt`       | Example prompts (image, motion, text-video, flow) |
| `output/`             | Saved media (images, videos, audio, slide-deck PDFs) |
| `debug/`              | Step screenshots (only with `--debug`) |

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

## NotebookLM (podcasts & slide decks)

`notebooklm_gen.py` creates a notebook, adds sources, and generates **Audio
Overviews (podcasts)** and **Slide Decks** via their Customize popovers — in any
output language and with a steering prompt — then downloads the `.m4a` / `.pdf`.

```bash
# Paste text as a source, make a Russian audio overview with a focus prompt
python3 notebooklm_gen.py --source-text "..." --audio --language Russian \
    --instructions "Focus on the engineering challenges"

# Let NotebookLM web-search its own sources, then make audio + slides
python3 notebooklm_gen.py --discover "history of the Voyager program" --audio --slides

# Add files as sources (PDF/audio/etc. uploaded; .txt/.md/.csv pasted)
python3 notebooklm_gen.py --source-file paper.pdf --slides --slides-prompt "10 concise slides"

# Operate on an existing notebook / just re-download ready artifacts
python3 notebooklm_gen.py --notebook <url> --download-only --audio --slides
```

Sources: `--source-text`, `--source-url` (×N), `--source-file` (×N),
`--discover "query"`. Artifacts: `--audio`, `--slides`, `--language`,
`--instructions`, `--slides-prompt`, `--audio-format` (Deep Dive/Brief/Critique/
Debate), `--audio-length` (Short/Default/Long). Verified: an ~18-min Russian
Audio Overview (`.m4a`) and a 5-page Slide Deck (`.pdf`).

### Slide-deck quality checks (optional)

NotebookLM occasionally (a) slips a **Ukrainian-only letter** (`і ї є ґ`) into a
"Russian" slide, or (b) invents a **fake decorative QR code** (always bogus). Two
opt-in checks run after the slides download and **exit `9`** if they trip, so you
can regenerate:

```bash
python3 notebooklm_gen.py --source-file article.ru.md --slides \
    --language Russian --check-ru-slides --check-qr
```

- `--check-ru-slides` — OCRs each page (`tesseract -l eng+rus+ukr`) and reports
  words containing Ukrainian-only letters. Requires `pdftoppm` + `tesseract` with
  the `rus` & `ukr` language packs. (OCR of stylized Latin text can add a few
  false positives — the offending words are logged so you can judge.)
- `--check-qr` — renders each page and flags any that contain a **decodable** QR
  code (`cv2.QRCodeDetector`). Requires `pdftoppm` + `opencv-python-headless`.

## YouTube upload (no Data API)

`youtube_upload.py` drives **YouTube Studio** (studio.youtube.com) through the same
logged-in Camoufox session — Create → Upload videos → fill Details → set
visibility → Save. No Data API, no OAuth, no API keys. Point `--metadata` at the
JSON that `video_maker` emits (`{title, description, tags[]}`); explicit flags
override it. **Default visibility is `private`** — nothing goes public unless you
pass `--visibility public`.

```bash
# Upload a video_maker bundle as a private draft (safe default)
python3 youtube_upload.py \
    --video     ../marketmaker/video_maker/output/SLUG/SLUG.mp4 \
    --metadata  ../marketmaker/video_maker/output/SLUG/SLUG_metadata.json \
    --thumbnail ../marketmaker/video_maker/output/SLUG/SLUG_thumbnail.png \
    --visibility private --debug

# Fully manual, unlisted
python3 youtube_upload.py --video clip.mp4 --title "Hi" \
    --description "..." --tags "a,b,c" --visibility unlisted
```

Flags: `--video` (required), `--metadata`, `--thumbnail`, `--title`,
`--description`, `--tags` (comma-separated), `--visibility` (private/unlisted/
public), `--made-for-kids` (default: not for kids), `--timeout`, `--headless`,
`--keep-open`, `--debug`. Exit codes: `0` ok · `2` bad args · `3` not logged in ·
`4` couldn't start upload · `5` details step failed · `6` couldn't finish.

> Studio's web-component UI changes often; if a step misses, run with `--debug`
> and inspect `debug/yt_*.png` — each step is screenshotted, and failures emit a
> shadow-DOM deep-dump of the live controls.

## AI Studio speech (STT / TTS / voice replication)

`aistudio_speech.py` turns audio into text, text into audio, and can replicate
**your own voice**, using the documented **Gemini API** with an AI Studio key and the
`google-genai` SDK. It is the one GAIA tool that does **not** use the logged-in
Camoufox session and never drives the AI Studio web UI (that would be fragile and
against the grain of Google's terms for a workflow that handles voice biometrics).
The "no API keys" line above is true for every other tool; this one needs a key *you*
create. Background and sources: [`docs/aistudio-speech-research.md`](docs/aistudio-speech-research.md).

| Feature | Model | Notes |
|---|---|---|
| Speech-to-text | `gemini-3.5-transcribe` | 85+ languages incl. `ru-RU`, optional speaker diarization (`spk_1`...), word timestamps, smart mode, custom vocabulary |
| Text-to-speech | `gemini-3.8-flash-lite-tts` (default), `gemini-3.8-flash-tts` | 30 prebuilt voices + the Voice Library, style control, inline tags such as `<short pause>`, WAV 24 kHz mono |
| Voice replication | same TTS models | 10-30 s reference + a spoken consent statement, same speaker; Google verifies both before creating the voice |

### Setup

```bash
python3.11 -m venv venv && source venv/bin/activate     # Python 3.11-3.13, same as the rest of GAIA
pip install -r requirements-aistudio.txt                # google-genai>=2.25.0,<3 (needs ffmpeg + ffprobe on PATH)
```

1. **Create the key** in [Google AI Studio](https://aistudio.google.com/apikey) on a Google
   Cloud project **with billing linked** (the paid tier). On the free tier ("Unpaid
   Services") Google may use your content to improve its products and humans may review it;
   a voice sample is biometric data, so use the paid tier for voice samples and for any
   private audio. No API call reveals the tier, so tell GAIA once: `GAIA_AISTUDIO_PAID_PROJECT=1`.
2. **Store the key outside the repo**, never in a file inside it:
   ```bash
   mkdir -p ~/.config/gaia && chmod 700 ~/.config/gaia
   printf 'GEMINI_API_KEY=your-key\nGAIA_AISTUDIO_PAID_PROJECT=1\n' > ~/.config/gaia/aistudio.env
   chmod 600 ~/.config/gaia/aistudio.env     # the file is refused unless its mode is 0600
   ```
   or export `GEMINI_API_KEY` (the environment wins). The key is never printed, logged or
   written by GAIA, and it is redacted from every error message. (`XDG_CONFIG_HOME` is honored;
   `GAIA_CONFIG_DIR` overrides the whole directory.)
3. `python3 aistudio_speech.py check` — verifies the key, SDK, ffmpeg and that the three models
   and the Voices endpoint are reachable (no cost). `check --dry-run` makes no network call.

### Commands

```bash
# Prebuilt and library voices (free to list)
python3 aistudio_speech.py voices list --lang ru-RU --type prebuilt

# Text -> speech. --out-dir is required and must be outside this repo.
python3 aistudio_speech.py tts --text-file script.txt --voice Kore --style "calm, warm" \
    --out-dir /abs/path/out                       # -> tts-<hash>.wav (24 kHz mono 16-bit)
python3 aistudio_speech.py tts --text-file script.txt --voice Kore --out-dir /abs/out --dry-run   # request shape + cost estimate, text elided
python3 aistudio_speech.py tts --text-file talk.txt --speaker Anna=Kore --speaker Boris=Puck \
    --out-dir /abs/out                            # lines "Anna: ..." / "Boris: ..." -> one request per turn, stitched
python3 aistudio_speech.py tts --text-file book.txt --voice me --format mp3 --out-dir /abs/out   # long text is chunked and stitched

# Speech -> text (private audio: absolute path outside the repo)
python3 aistudio_speech.py stt --input /abs/call.m4a --lang ru-RU --diarize --words \
    --format txt --format srt --format json --out-dir /abs/out
python3 aistudio_speech.py stt --input /abs/call.m4a --out-dir /abs/out --dry-run               # preflight + request shape, nothing sent
```

`stt` flags: `--lang` (BCP-47 hint, repeatable; default auto-detect), `--diarize` (up to 8
speakers, experimental from 3), `--words` (word timestamps, needed for `srt`/`vtt`, may reduce
accuracy), `--smart` (no disfluencies, formatted numbers), `--term`/`--vocab-file` (custom
vocabulary). Limits enforced locally: 1 h per file, **30 min with `--diarize`/`--words`**, and
the combinations the API rejects (vocabulary or `--smart` together with diarization/timestamps).
Audio under ~14 MB goes inline; larger files go through the Files API and are **deleted
afterwards** (they would live 48 h). Google publishes no Russian WER or diarization benchmark:
measure on your own non-private audio before relying on it.

`--api generate-content` (default) or `--api interactions` (env `GAIA_AISTUDIO_API`) picks the
request surface; Google calls Interactions GA and recommended, `generateContent` legacy but
supported. Interactions requests are always sent with `store=false` (otherwise Google keeps
them for up to 55 days). The code for both lives behind `aistudio/client.py`.

Exit codes: `0` ok · `2` bad args · `3` no key / auth failed · `4` audio or privacy preflight
failed · `5` API refused (region, quota, consent mismatch, server error) · `6` output not produced.

### Replicating your own voice

Only your own voice. Google requires two recordings by the **same adult speaker** — a clean
10-30 s reference and the fixed consent statement for the language read verbatim — and checks
that the speakers match.

```bash
python3 aistudio_speech.py consent-text --lang ru-RU      # prints the exact statement + a recording checklist
# record both clips in one sitting, same mic and room, then (absolute paths, outside the repo):
python3 aistudio_speech.py voices replicate --reference /abs/ref.wav --consent /abs/consent.wav \
    --lang ru-RU --alias me --no-store --i-am-the-speaker --dry-run     # preflight both clips, print the request
python3 aistudio_speech.py voices replicate ... --no-store --i-am-the-speaker --audition-out-dir /abs/check
python3 aistudio_speech.py tts --text-file script.txt --voice me --out-dir /abs/out
python3 aistudio_speech.py voices list --local             # your aliases (never shows a key)
python3 aistudio_speech.py voices delete me                # when the project is over (--dry-run shows what would go)
```

* `--i-am-the-speaker` is mandatory (plus a typed confirmation on a TTY). Both clips are checked
  with ffprobe (reference 10-30 s, consent a few seconds, clipping, level mismatch) and converted
  to 24 kHz mono 16-bit WAV in a temp dir outside the repo that is removed afterwards.
* `--store` / `--no-store` is always sent explicitly. **Start with `--no-store`** (default): you get
  a `voicekey_...` valid 7 days and Google keeps nothing. `--store` keeps a `voice_...` at Google
  (200 per project, expires 1 year after last use); use it only for a voice you will reuse, and delete it afterwards.
* Local state in `~/.config/gaia/` (mode 0600, never in the repo): `voices.json` (alias -> id/key; a
  `voicekey_...` is a secret like the API key) and `voice-consent.jsonl` — an append-only consent log with
  the SHA-256 of the reference and consent clips, the consent language, the SHA-256 of the statement, a
  timestamp and the voice id. No audio, no transcript.
* Output carries Google's SynthID watermark and, for replicated voices, C2PA credentials. A single unmodified
  request is written byte for byte; chunk stitching or MP3/FLAC conversion changes the file (GAIA warns).
  Do not present synthetic speech as human; follow each platform's labelling rules for synthetic audio.
* Prohibited: cloning anyone else's voice, impersonation, misrepresenting provenance (Google's Generative AI
  Prohibited Use Policy). Custom and replicated voices cannot be mixed in one multi-speaker request, hence the
  turn-by-turn `--speaker` mode.

### Privacy rules (enforced in code)

* **Key:** `GEMINI_API_KEY` or `~/.config/gaia/aistudio.env` (mode 0600; a key file inside the repo is refused).
  `.gitignore` also blocks `.env*`, `aistudio.env`, `*.voicekey`, `voices.json` and audio files as a second guard.
* **Private audio:** only **absolute** input paths that resolve (symlinks followed) **outside** this repo and the
  super-project that embeds it. There is no override flag.
* **Outputs:** only to the `--out-dir` you choose, outside the repo (never `output/` or `debug/`); files are
  created with mode 0600, named from a hash or your `--name` slug — never from the source file or a speaker.
* **Logs:** lengths, durations, model and error codes only. No transcript text, no prompt text, no file names of
  private audio; `--dry-run` prints request shapes with text and audio elided and voice keys masked.
* `voices replicate` and `stt` need `GAIA_AISTUDIO_PAID_PROJECT=1` or an explicit `--accept-unpaid-terms` per run.

### Region limits

Voice replication in AI Studio is **not available in Illinois, Texas, the EEA, the UK, Switzerland and India**
(Google's launch post). The API reference does not say whether the API follows the same rule, so the only
reliable test is one `voices replicate` from your real location: a region refusal comes back as exit code `5`.
The Gemini API and AI Studio as a whole are only available in the
[listed countries](https://ai.google.dev/gemini-api/docs/available-regions) (Russia and Belarus are not on it).
Do not route around a regional block with a VPN: that misrepresents your location. Check your account's country
before recording anything.

### Cost notes (paid tier list prices, 2026-10-19; `tts --dry-run` and `stt --dry-run` print an estimate)

* STT `gemini-3.5-transcribe`: about **$0.005 per audio minute** (input $0.003 + text output $0.002).
* TTS output: `gemini-3.8-flash-tts` **$0.00225 per 10 s**, `gemini-3.8-flash-lite-tts` **$0.0015 per 10 s**
  (25 audio tokens/s) plus a small text-input charge. **Both TTS prices double on 2027-01-01.**
  Creating a replicated voice has no separate documented charge; synthesis with it is billed as normal TTS.
* The free tier is "free of charge" but the content may be used to improve Google products (see Setup).
* A TTS call returns at most ~10.9 minutes of audio (16,384 output tokens), so long scripts are chunked
  (`--max-chars`, default 2500) and the WAV chunks are stitched with a short gap (`--gap-ms`).

### Tests

```bash
pip install -r requirements-aistudio-dev.txt
python -m pytest tests/aistudio          # offline: no network, no key, synthetic audio only
GAIA_LIVE=1 python -m pytest tests/aistudio/test_live.py -v    # opt-in, needs a key; synthetic TTS -> STT round trip
```

The offline suite covers config and the 0600 rule, key redaction, the repo-path guard, request builders against
golden JSON, the real SDK's wire format through a mocked transport, response parsers, error mapping, the consent
table (byte for byte against Google's published statements) and every CLI `--dry-run` path. Live tests never
create a replicated voice; the first replication is a manual step by the account holder.

## How it works

1. **Cookies** — `cookies_firefox.py` copies `cookies.sqlite` (+ WAL journal) so it
   reads even while Firefox is open, pulls every `*.google.com` cookie, and
   normalizes them: this Firefox build stores `expiry` in **milliseconds** (auto
   converted to seconds), and all cookies are mapped to `SameSite=Lax` (every
   `*.google.com` host is same-site for the Gemini app, so Lax is always sent and
   avoids Playwright's `SameSite=None`-requires-`Secure` rule).
2. **Launch** — `make_camoufox()` opens a **persistent** Camoufox context with a
   **pinned fingerprint** (`.camoufox_profile/` + `.camoufox_fp.pkl`). Firefox
   cookies are injected only to bootstrap the first run; afterwards GAIA reuses its
   own session, so the same device is presented every time and your Firefox login
   is never disturbed again.
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

## License

[MIT](LICENSE) — fork it and adapt to your brand with any AI agent. See the
**Disclaimer** above: personal/educational use, your own account, at your own risk.
