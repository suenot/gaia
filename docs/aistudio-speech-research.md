# Google AI Studio speech: STT, TTS and voice cloning for gaia

Research note, 2026-10-19. Scope: what Google offers today for speech-to-text,
text-to-speech and cloning the account holder's own voice, which transport gaia
should use for each, and a proposed module layout. All facts below were read from
Google's own pages on 2026-10-19 unless marked "unverified". No code was changed
and nothing was submitted to any Google service while writing this note.

## 1. Summary

- Everything gaia needs exists in the **Gemini API** (AI Studio key, plain HTTPS):
  dedicated STT (`gemini-3.5-transcribe`, with diarization and word timestamps),
  dedicated TTS (`gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts`), and
  **voice replication** (`POST /v1beta/voices`, `type: "replicated"`), which went
  GA on 2026-09-22. No browser automation is needed for any of it.
- **Russian voice cloning is supported today**, by the Gemini API / AI Studio
  (Russian consent statement is published, TTS covers Russian). Google Cloud's
  separate product, Chirp 3 Instant Custom Voice, also lists `ru-RU`, but it is
  allowlist-only (sales contact).
- Replication is few-shot (a 10 to 30 s clip plus a spoken consent clip), not
  training on hours of audio. No self-serve "trained custom voice" product was
  found in the current docs.
- **Blocker to check before anything else:** Google's launch post says voice
  replication through AI Studio is not available in Illinois, Texas, the EEA, the
  UK, Switzerland and India. The Gemini API docs do not state whether the API has
  the same restriction. Russia and Belarus are absent from the list of regions
  where AI Studio and the Gemini API are available at all. The account holder's
  country and the IP they call from decide whether this works.
- The Gemini API free tier is "Unpaid Services": Google may use the content to
  improve products and human reviewers may read it. A voice sample is biometric
  data, so use a project with active billing (Paid Services, content not used for
  training) for cloning, and for any private audio.
- Recommended transport: **Gemini API with an AI Studio key for all three
  features.** Do not drive the AI Studio web UI through Camoufox for this.

## 2. What exists today

### 2.1 Speech-to-text

| Item | Detail |
|---|---|
| Model | `gemini-3.5-transcribe` (files), `gemini-3.5-transcribe-live` (streaming). GA 2026-08-26. |
| Languages | 85+ auto-detected, with code-switching. `ru-RU` and `uk-UA` are in the BCP-47 table. `language_codes` can hint the language. |
| Diarization | `mode: {type: "verbatim", diarization_mode: "speaker"}`, labels like `spk_1`, up to 8 speakers, attribution for 3 or more is "experimental". File mode only, not in live mode. |
| Timestamps | `timestamp_granularities: ["word"]`, start and end offsets per word. Docs warn it "may degrade overall transcription accuracy". |
| Other modes | `smart` mode (removes disfluencies, formats numbers) cannot be combined with diarization or timestamps. `custom_vocabulary` (up to 1,000 terms, best at 100) cannot be combined with diarization or timestamps. |
| Limits | Up to 1 h per request, 30 min when diarization or word timestamps are on. Live: 10 min per session. Request size 20 MB inline, otherwise Files API (files kept 48 h, delete after use). |
| Formats | wav, mp3, aiff, aac, ogg, flac, mpeg, m4a, l16, opus, alaw, mulaw, webm. Live input is raw 16-bit PCM, `audio/pcm;rate=16000`. |
| Price (paid) | About $0.003/min audio in plus $0.002/min text out, about $0.005/min blended. Live about $0.009/min. Free tier: "Free of charge" (content used by Google). |
| Russian quality | **No Google-published Russian WER or diarization benchmark.** Headline numbers in reviews are multilingual FLEURS and secondhand. Must be measured on our own audio (section 8). |
| Older route | General audio understanding with a normal Gemini model (`gemini-3.8-flash`) gives MM:SS segment timestamps, speaker labels and emotion via structured output. Less precise than the dedicated model. |
| Cloud alternative | Cloud Speech-to-Text v2 Chirp 3 lists `ru-RU` for transcription, but its diarization language list does **not** include `ru-RU` (de-DE, en-*, es-*, fr-*, hi-IN, it-IT, ja-JP, ko-KR, pt-BR, cmn-Hans-CN only), diarization works in BatchRecognize only, and word-level timestamps are listed as unsupported. Needs a GCP project with billing. |

### 2.2 Text-to-speech

| Item | Detail |
|---|---|
| Models | `gemini-3.8-flash-tts` (fidelity, dialects, long-form), `gemini-3.8-flash-lite-tts` (cheap, fast, recommended by Google for voice replication). Older: `gemini-3.1-flash-tts-preview`, `gemini-2.5-pro-preview-tts`. GA 2026-09-22. |
| Languages | Flash: 130+, Flash-Lite: 100+. Russian is in both. Language is auto-detected from the text; an optional `speechConfig.languageCode` exists on generateContent (30 listed values, `ru-RU` among them). |
| Voices | 30 prebuilt (Zephyr, Puck, Charon, Kore, Fenrir, ...), Extended Voice Library (`GET /v1beta/voices`, filter by `language_code`, gender, pitch, persona, `type`), **Voice design** (voice from a text description), **Voice replication** (voice from audio). |
| Style control | Turn level `speech_metadata.style` (for example "whispered urgently"), inline tags `<laugh> <sigh> <short pause> <long pause>` etc. (keep tags in English even for non-English text), CAPS for emphasis, `\|backchannel\|` between speakers. Text is read verbatim, so directions go in `style`. |
| Multi-speaker | Up to 2 speakers per request, prebuilt voices only. Custom or replicated voices must be synthesized turn by turn and concatenated. |
| Output | Unary: WAV (`audio/wav`, 24 kHz mono 16-bit, RIFF header). Streaming: raw `audio/l16`. Also `audio/mulaw`, `audio/alaw`; `sample_rate` 24000/16000/8000. The TTS guide documents no MP3 or Opus (the generateContent REST enum lists `AUDIO_MP3` and `AUDIO_OGG_OPUS` but the Python SDK's `GenerateContentConfig` has no field for it): transcode locally. |
| Limits | Input 8,192 tokens, output 16,384 tokens. At 25 audio tokens/s that is about 10.9 min of audio per call (derived), so chunk long scripts. |
| Price (paid) | Flash: $9/M audio tokens = $0.00225 per 10 s. Flash-Lite: $6/M = $0.0015 per 10 s. Both **double on 2027-01-01**. Free tier: "Free of charge" (content used by Google). Batch and Flex are 50% cheaper. |
| Live API | `gemini-3.8-live` speaks with prebuilt voices (`prebuilt_voice_config.voice_name`). Custom or replicated voices in Live API: not documented. Sessions: audio-only 15 min. Not needed for gaia (offline narration). |

API surface note (corrected 2026-10-19, see section 11): the 3.5/3.8 guides lead
with the **Interactions API** (`POST /v1beta/interactions`). Google's Interactions
overview says it has been Generally Available since June 2026 and is recommended
for all new projects; `generateContent` is called legacy but "remains fully
supported". (The Voices API reference page itself is still labeled Beta, and every
endpoint, including `interactions`, lives under `/v1beta/`.) Both surfaces exist
for STT and TTS; the response shapes differ (Interactions: `steps[].content[]` and
`word_info` annotations; generateContent: `candidates[].content.parts[]` with
`audioTranscription {text, speakerLabel, words[{word, startOffset, endOffset}]}`).
The transcribe guide documents only the Interactions request; the generateContent
request is `generationConfig.audioTranscriptionConfig {languageCodes,
customVocabulary, wordTimestamp, diarization, mode: VERBATIM|SMART}` (REST
reference). The Voices endpoint is the same for both. Interactions stores requests
by default (55 days on the paid tier, 1 day on the free tier) unless `store=false`.

### 2.3 Custom voice: three different things

| | Gemini voice replication | Chirp 3 Instant Custom Voice (Cloud TTS) | Trained custom voice |
|---|---|---|---|
| Where | AI Studio web UI and Gemini API (`/v1beta/voices`) | Google Cloud Text-to-Speech API (`v1beta1`), Vertex AI Studio demo | No current product found. The old Cloud "Custom Voice" docs path returns 404 (2026-10-19). Treat as not available self-serve. |
| Access | Self-serve, any AI Studio key, subject to region blocks | **Allowlist, "contact a member of the sales team"** | n/a |
| Russian | Yes: consent statement for `ru-RU` published, TTS supports Russian | Yes: `ru-RU` in the 30-locale list, synthesizes natively | n/a |
| Sample | Reference 10 to 30 s, consent clip (length not stated), 24 kHz mono 16-bit WAV recommended | Reference up to 10 s (aim for 10 s), consent up to 10 s, any sample rate; LINEAR16, PCM, MP3, M4A | n/a |
| Result | `voice_...` ID stored by Google (200 per project, 1 year TTL reset on use) or `voicekey_...` stateless key (7 day TTL) | `voiceCloningKey` string kept client side, no limit on count, 10 keys/min/project | n/a |
| Model used | `gemini-3.8-flash-tts` or `-flash-lite-tts` | Chirp 3 | n/a |
| Cross-language | Not documented. Test (section 8). | Only an `en-US` key is documented to transfer, to de-DE, es-US, es-ES, fr-CA, fr-FR, pt-BR. No transfer listed for a `ru-RU` key. | n/a |
| Regions | AI Studio: not in IL, TX, EEA, UK, CH, India. API: not stated. | `global, us, eu, asia-southeast1, asia-northeast1, europe-west2` | n/a |
| Price | No separate charge documented for creating a voice; synthesis at the TTS token price. Verify with the first call. | $60 per 1M characters ($0.00006/char), no free tier, billing required | n/a |
| Provenance | SynthID watermark on all Gemini audio output, C2PA credentials on replicated voices | Not stated | n/a |
| Status | Gemini API docs: GA. Cloud TTS docs: the same models "available in Preview through the Gemini Enterprise API only... aren't available through the Cloud Text-to-Speech API". | Restricted access, no GA statement | n/a |

Distinction asked for: both existing options are **instant / few-shot** cloning.
Neither trains on hours of recordings. If the replicated voice proves too weak,
the options are Chirp 3 ICV (allowlist) or a non-Google local model, not a Google
fine-tuning product.

## 3. Voice replication: exact requirements (Gemini API)

Source: voice-replication guide, Voices API reference, launch post.

1. **Two recordings from the same adult speaker**, same microphone and room
   (Google runs a speaker-verification check between them before creating the
   voice):
   - `source_audio`: 10 to 30 s of clean, natural speech. Quiet room, no music,
     no overlapping voices, no echo. 24 kHz mono 16-bit PCM WAV recommended;
     `audio/mpeg` is also accepted by the API schema.
   - `consent_audio`: the speaker reading the **fixed consent statement for the
     chosen language** verbatim. Custom wording is not allowed. Statement for
     Russian (`ru-RU`):

     > Я являюсь владельцем этого голоса и даю согласие Google на использование этого голоса для создания модели синтетического голоса.

     English (`en-US`, also `en-GB`, `en-IN`, `en-AU`):
     "I am the owner of this voice and I consent to Google using this voice to
     create a synthetic voice model." Statements exist for 30 locales (ar-XA,
     bn-IN, zh-CN, nl-NL, en-*, fr-FR, fr-CA, de-DE, gu-IN, hi-IN, id-ID, it-IT,
     ja-JP, kn-IN, ko-KR, ml-IN, mr-IN, pl-PL, pt-BR, ru-RU, es-ES, es-US,
     ta-IN, te-IN, th-TH, tr-TR, vi-VN).
2. **Request**: `POST https://generativelanguage.googleapis.com/v1beta/voices`,
   header `x-goog-api-key`, body
   `{"store": true|false, "voice": {"model": "gemini-3.8-flash-tts", "type": "replicated", "display_name": "...", "replicated": {"source_audio": {"mime_type": "audio/wav", "data": "<base64>"}, "consent_audio": {"mime_type": "audio/wav", "data": "<base64>"}}}}`.
   SDK: `client.voices.create(...)` in `google-genai >= 2.25.0` (latest on PyPI
   2.31.0, Python >= 3.10).
3. **Storage mode**: the guide says stateful (`store=True`) is the default, the
   API reference says the default is `false`. Always pass `store` explicitly.
   Stateful: `voice_...` ID, 200 per project (shared with designed voices), 1 year
   from last use. Stateless: `voicekey_...`, an encrypted self-contained key,
   7 days, nothing kept by Google. Management: `voices.list/get/delete`.
4. **Use**: pass the `voice_...` ID or `voicekey_...` key in
   `speech_config` (Interactions: `[{"voice": id}]`; generateContent:
   `voiceConfig.voice`). One replicated voice per request (no multi-speaker mix).
5. **Eligibility**: 18+, an available region
   ([available regions](https://ai.google.dev/gemini-api/docs/available-regions)),
   Prohibited Use Policy. Creation errors named in the reference:
   `INVALID_ARGUMENT`, `RESOURCE_EXHAUSTED` (quota). The failure code for a
   consent mismatch is not documented.
6. **Free tier**: TTS pricing says "Free of charge" for the free tier, but the
   docs do not say whether `voices.create` works on it. If it does, the content
   is used to improve Google products (Terms, "Unpaid Services").

Chirp 3 Instant Custom Voice, for reference: `POST
https://texttospeech.googleapis.com/v1beta1/voices:generateVoiceCloningKey`
with `reference_audio`, `voice_talent_consent`, `consent_script`,
`language_code`; OAuth bearer token plus `x-goog-user-project`; synthesis via
`.../v1beta1/text:synthesize` with `voice.voice_clone.voice_cloning_key`. Needs a
GCP project with billing and the allowlist.

## 4. Web UI versus API

**What the AI Studio UI does** (from Google's docs and launch post; not inspected
by us, see below): `aistudio.google.com/generate-speech` is a voice workspace with
the Voice Library (browse and audition), Voice Design, and Voice Replication
(record or upload the reference and consent clips in the browser, preview, copy
the `voice_...` ID). `prompts/new_chat?model=gemini-3.5-transcribe` runs STT in
the chat playground and `/live?model=gemini-3.5-transcribe-live` does live
transcription. We did not open the UI: it needs a logged-in session and the
unauthenticated URL just redirects to Google sign-in.

**Parity**: the API does everything the UI does for speech (list, create, get,
delete voices; TTS; STT). The only UI-only conveniences are interactive
auditioning and in-browser recording; both are replaced by local files.

**Terms on automating the UI**
- Gemini API Additional Terms (effective 2026-03-23): AI Studio and the Gemini API
  are "for developers building with Google AI models for professional or business
  purposes, not for consumer use"; must be 18+; Prohibited Use Policy applies; do
  not bypass safety measures.
- Google APIs Terms of Service, "Permitted access": "You will only access (or
  attempt to access) an API by the means described in the documentation of that
  API ... You will not misrepresent or mask either your identity or your API
  Client's identity."
- Google Terms of Service (effective 2026-07-30): do not "abuse, harm, interfere
  with, or disrupt" services, for example by "bypassing our systems or protective
  measures", and no automated access in violation of machine-readable
  instructions. AI Studio's `robots.txt` disallows `/prompts/`, `/app`,
  `/library`, `/projects`, `/usage` etc. (it does not list `/generate-speech`).
- gaia's own README already warns that driving Google products with an
  anti-detect browser may breach their Terms and can get the account
  rate-limited or suspended. Putting a voice-biometric workflow behind that risk
  is a poor trade when a documented API exists. Not legal advice.

## 5. Feature by transport

Legend: yes = works and is the documented route; partial = works with limits;
no = not available; risk = possible but against the Terms or fragile.

| Feature | Gemini API + AI Studio key | AI Studio UI via Camoufox | Google Cloud (GCP project + billing) |
|---|---|---|---|
| STT, files, Russian | yes (`gemini-3.5-transcribe`) | risk (chat playground, no stable selectors, uploads) | yes (Chirp 3, v2 API) |
| STT diarization, Russian | yes, up to 8 speakers (verify RU quality) | risk | no for `ru-RU` in Chirp 3 |
| STT word timestamps | yes (30 min cap) | risk | no (Chirp 3 lists word-level timestamps as unsupported; utterance timestamps only in streaming) |
| STT live streaming | yes (`gemini-3.5-transcribe-live`, WebSocket, 10 min) | no (needs mic) | yes (StreamingRecognize) |
| TTS prebuilt voices, Russian | yes | risk | partial (Chirp 3 HD, Gemini-TTS 3.1; 3.8 not in Cloud TTS API) |
| TTS style control, tags | yes | risk | partial |
| TTS multi-speaker (2) | yes | risk | partial |
| Voice Library list/filter | yes (`GET /v1beta/voices`) | risk | no |
| Voice design | yes | risk | no |
| Voice replication, Russian | yes (region permitting) | risk | partial (Chirp 3 ICV, allowlist) |
| Manage/delete voices | yes | risk | n/a |
| Needs paid project for data protection | yes, link billing (Tier 1) | n/a (consumer session, unpaid terms unless project billed) | yes, billing mandatory |
| Setup cost | key in AI Studio (manual, one time) | existing Camoufox login | project, billing, sales allowlist for ICV |
| Stability | versioned REST, docs | breaks on UI change | versioned REST |
| Terms posture | documented | against the grain | documented |

## 6. Consent and legal requirements for cloning a voice

- **Only your own voice.** Google requires a verbal consent recording by the
  voice owner that matches the reference speaker, checked before the voice is
  created. Do not try to defeat it, and do not create a voice of anyone else, even
  with their verbal permission relayed by you. If a second person's voice is ever
  needed, that person records and submits their own consent clip, and a written
  consent record is kept.
- **Generative AI Prohibited Use Policy**: no use of "personal data or biometrics
  without legally-required consent", no "impersonating an individual (living or
  dead) without explicit disclosure, in order to deceive", no "misrepresenting
  the provenance of generated content by claiming it was created solely by a
  human, in order to deceive". Cloning yourself with disclosure where audiences
  could be misled is in bounds; passing it off as live human speech is not.
- **Provenance**: output is SynthID-watermarked and the replicated voice carries
  C2PA credentials. Do not strip them. Follow each publishing platform's rules
  for synthetic or altered content labels.
- **Biometric data handling**: a voice sample is biometric data. Use Paid Services
  (billing-linked project): then Google does not use it to improve products,
  though prompts and responses are logged for a limited time for abuse
  monitoring (zero data retention would require Vertex-class enterprise terms).
  Prefer `store=False` (7 day key) when the voice is only needed for a one-off
  job; use `store=True` only for a voice reused over months, and delete it when
  the project ends (`voices.delete`).
- **Jurisdiction**: AI Studio replication is blocked in Illinois, Texas, EEA, UK,
  Switzerland, India (Google gives no reason). Confirm the account's country and the machine's IP are outside
  those areas and inside an
  [available region](https://ai.google.dev/gemini-api/docs/available-regions).
  Calling through a VPN to get around a regional block would be misrepresenting
  your location: do not.
- **Third-party audio**: recordings of other people (calls, interviews) contain
  their voices. Sending them to a free-tier endpoint exposes them to Google's
  product-improvement use and human review. Private audio should be processed locally
  by default and an external service needs explicit authorization for that
  service and payload. Gemini STT is opt-in per job, paid tier only, never the
  default for private calls.
- **Credentials**: Google APIs Terms say developer credentials "may not be
  embedded in open source projects". gaia is public, so keys never go in the repo.

## 7. Recommended architecture for gaia

Principle: this is the one gaia component that does **not** use the logged-in
Camoufox session. State that plainly in the README so the "no API keys" line
about the other tools stays accurate: the speech module uses a documented API and
a key the user creates themselves.

### 7.1 Layout

Follows gaia's convention of flat root scripts that import helpers by module
name (`gemini_image_gen.py`, `notebooklm_gen.py`), with a small package for the
new code:

```
gaia/
  aistudio_speech.py        # CLI entry (argparse subcommands, exit codes like the other scripts)
  aistudio/
    __init__.py
    config.py               # key + path resolution, redaction, never prints secrets
    client.py               # thin wrapper over google-genai (or plain HTTPS); one place to switch
                            #   between generateContent and Interactions; retries, error mapping
    audio.py                # ffprobe/ffmpeg preflight, resample to 24 kHz mono WAV, strip RIFF header,
                            #   concat PCM, chunk text, SRT/VTT/JSON writers
    stt.py                  # transcribe(): Files API upload (deleted after), diarization, word timestamps
    tts.py                  # speak(): chunking, voice resolve, per-turn synthesis for custom voices
    voices.py               # list/get/delete, local alias registry, consent text table (30 locales)
    voice_clone.py          # create_replicated(): preflight, attestation, create, local consent log
    privacy.py              # refuse paths inside the repo, redact logs, output dir policy
  requirements-aistudio.txt # google-genai>=2.25,<3   (kept apart from camoufox/playwright pins)
  tests/aistudio/           # offline unit tests + opt-in live tests
```

Decision (implemented): `generateContent` is the default request surface and the
Interactions variant (`--api interactions`, always with `store=false`) sits behind
`client.py`, so a switch does not touch the callers. Both are documented by Google.

### 7.2 CLI

```
python3 aistudio_speech.py check                                  # key present? model list reachable? no cost
python3 aistudio_speech.py voices list [--lang ru-RU] [--type prebuilt|prompted|replicated]
python3 aistudio_speech.py voices delete <alias-or-id>
python3 aistudio_speech.py consent-text --lang ru-RU              # prints the exact statement + recording checklist
python3 aistudio_speech.py voices replicate \
    --reference /abs/path/ref.wav --consent /abs/path/consent.wav \
    --lang ru-RU --alias me --store|--no-store --i-am-the-speaker
python3 aistudio_speech.py tts --text-file script.txt --voice me|Kore|voice_... \
    --style "calm, warm" --model flash-lite --out-dir /abs/out [--format wav|l16|mp3]
python3 aistudio_speech.py stt --input /abs/path/call.m4a --lang ru-RU \
    --diarize --words --format srt|json|txt --out-dir /abs/out
python3 aistudio_speech.py stt-live --mic | --input file     # optional, phase 3
```

Exit codes in the gaia style: `0` ok, `2` bad args, `3` no key or auth failed,
`4` audio preflight failed, `5` API refused (region, quota, consent mismatch),
`6` output not produced.

### 7.3 Config and secrets (nothing personal in git)

- Key: env `GEMINI_API_KEY` (the SDK default), else the file
  `${XDG_CONFIG_HOME:-~/.config}/gaia/aistudio.env` containing
  `GEMINI_API_KEY=...`. The loader refuses a world-readable file (mode must be
  `0600`), never prints the key, masks it in logs and errors.
- Never read a key from inside the repo working tree. Add `.env*`,
  `aistudio.env`, `*.voicekey`, `voices.json` to `.gitignore` as a second guard.
- Voice registry (alias to ID/key, model, locale, created, expiry, store mode):
  `~/.config/gaia/voices.json`, mode `0600`. A `voicekey_...` is an encrypted
  voice profile: treat it as a secret, same as the key.
- Consent log (append-only, local): date, alias, locale, SHA-256 of the reference
  and consent files, statement text hash, voice ID, model. No audio, no transcript.
- Project: the key must belong to a Cloud project with active billing (check:
  AI Studio shows the project tier; Tier 1 needs only linked billing). `check`
  warns when it cannot confirm a paid project and `voices replicate` and `stt`
  require `--accept-unpaid-terms` to proceed on an unpaid key.

### 7.4 Private audio in and out

- Inputs are absolute paths outside the repo. `privacy.py` resolves symlinks and
  exits `4` if the path is inside the gaia git root (override only with
  `--allow-in-repo`, intended for synthetic test fixtures).
- Outputs default to `--out-dir` given by the caller; the fallback is `output/`
  (already git-ignored). Filenames are derived from a hash or caller-supplied
  slug, never from the source file name or speaker name.
- Transcripts and audio are never written to `debug/` or logs. `--verbose` logs
  sizes, durations, model, request IDs and error codes only.
- Uploaded files go through the Files API only above 20 MB or when reuse is
  needed, and are deleted in a `finally` block (they live 48 h otherwise).
- A tiny `--dry-run` prints the exact request shape (sizes, models, config) with
  no network call, so a job can be reviewed before private audio leaves the machine.

### 7.5 Guard rails in the cloning command

1. Print the consent statement for `--lang` and require the recordings to exist.
2. Local preflight with ffprobe: reference 10 to 30 s, mono-capable, no clipping,
   sensible loudness; consent clip short (about 5 to 10 s); warn if the two differ
   a lot in level (hint of a different mic/room). Convert both to 24 kHz mono
   16-bit WAV in a temp dir that is removed afterwards.
3. Require `--i-am-the-speaker` and a typed confirmation on a TTY. In
   non-interactive runs the flag alone is required.
4. Create the voice, store the result in the registry, write the consent log,
   synthesize one fixed audition sentence, and tell the user to listen and to
   `voices delete` if it is not right.
5. Refuse `--alias` collisions; print the expiry date for stateless keys.

### 7.6 Phases

1. `check`, `voices list`, `tts` with prebuilt voices, `stt` (cheap, no biometrics).
2. `voices replicate`, `tts --voice <alias>` with per-turn synthesis and
   stitching, audition command.
3. Optional: live STT, Batch API for long narration (50% cheaper), Cloud
   Chirp 3 ICV adapter only if replication is blocked or unsatisfactory.

## 8. Test plan

**Offline (CI-safe, no key, no audio of people)**
- Config precedence and permission refusal; key redaction in logs and exceptions.
- Path guard: in-repo path refused, symlink into repo refused, `--allow-in-repo`
  works for fixtures.
- Request builders against golden JSON for STT (language hint, diarization,
  word timestamps, incompatible combinations rejected locally: custom vocabulary
  with diarization or timestamps, smart mode with either), TTS (single, two
  speaker, tags in English), `voices.create` (store flag always explicit).
- Response parsers against recorded fixtures for both API surfaces; SRT/VTT/JSON
  writers; RIFF header strip and PCM concat produce a valid WAV of the summed
  duration; text chunker never exceeds the token budget.
- Consent table equals the published statements (ru-RU compared byte for byte
  with the doc text recorded in a fixture).

**Live, opt-in** (`GAIA_LIVE_TESTS=1`, a paid-tier test key, synthetic audio only)
1. `voices list --lang ru-RU` returns Russian voices (no cost).
2. TTS of a short Russian sentence with a prebuilt voice: WAV is 24 kHz mono,
   duration within expected range; round trip through `gemini-3.5-transcribe`
   gives WER under a threshold we set after the first run.
3. Two-speaker prebuilt TTS clip as the diarization fixture: STT returns two
   speaker labels, word offsets monotonic and within clip duration.
4. Error mapping: wrong key gives exit `3`; a bad model name gives a clear message.

**Manual, by the account holder only (never CI)**
1. Confirm region eligibility (country, IP, AI Studio shows the Voice Replication
   tab) and that the project is billed.
2. Record the Russian consent statement and a 20 to 30 s reference at one sitting,
   same mic and room. Run `voices replicate --no-store` first (7 day key), then
   `--store` if the result is good.
3. Evaluate: Russian sentence, English sentence (cross-language behavior is
   undocumented), numbers and proper names, a 5 minute narration chunked and
   stitched. Metrics: listening check, round-trip WER via STT, optional local
   speaker-embedding cosine between reference and output (an ECAPA-style model).
4. Check the output file carries the provenance metadata (C2PA/SynthID) and is not
   altered by our post-processing.
5. Delete the voice (`voices delete`) and the consent log entry's audio inputs from
   scratch directories when the project is finished.

**STT quality for Russian** (separate from cloning): run `gemini-3.5-transcribe`
on a few hard excerpts of the user's own non-private audio, compare to a trusted
transcript, and compare diarization against a local pipeline before relying on it.
Google publishes no Russian benchmark.

## 9. Open items and caveats

- The Gemini API doc does not say whether the AI Studio regional block on
  replication applies to the API. Test with one `voices.create` from the real
  location; the error code and message will answer it.
- Whether the free tier can call `voices.create` is undocumented; do not test it
  with a real voice (use a paid-tier project).
- Default of `store` differs between the guide (true) and the API reference
  (false). Always pass it.
- Consent clip length limit for Gemini replication is not documented (Cloud ICV
  says up to 10 s).
- Cross-language use of a Russian-consented voice for English output is
  undocumented for Gemini; Cloud ICV documents transfer only from `en-US`.
- Cloud pricing pages still label the 3.8 TTS models "(Preview)" while the Gemini
  API changelog calls them GA (2026-09-22). Access to the 2.5 models is limited
  to prior users (changelog, 2026-09-18), so new work should target 3.8.
- Prices for 3.8 TTS double on 2027-01-01.
- Live API with a replicated voice, and diarization quality beyond 2 speakers
  (marked experimental), are untested.
- The UI was not inspected; section 4 describes it from Google's documents.

## 10. Sources (all read 2026-10-19)

Gemini API
- Text-to-speech guide: https://ai.google.dev/gemini-api/docs/speech-generation
- Voice replication: https://ai.google.dev/gemini-api/docs/voice-replication
- Voice design: https://ai.google.dev/gemini-api/docs/voice-design
- Voices API reference: https://ai.google.dev/api/voices
- Gemini 3.8 Flash TTS model page: https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-tts
- Gemini 3.8 Flash-Lite TTS model page: https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-lite-tts
- Audio transcription guide: https://ai.google.dev/gemini-api/docs/transcribe
- Gemini 3.5 Transcribe model page: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe
- Live transcription: https://ai.google.dev/gemini-api/docs/live-api/live-transcribe
- Audio understanding: https://ai.google.dev/gemini-api/docs/audio
- Live API capabilities: https://ai.google.dev/gemini-api/docs/live-api/capabilities
- Pricing: https://ai.google.dev/gemini-api/docs/pricing
- Rate limits and tiers: https://ai.google.dev/gemini-api/docs/rate-limits
- Files API: https://ai.google.dev/gemini-api/docs/files
- Changelog (2026-08-26 Transcribe GA, 2026-09-22 TTS and Voices GA): https://ai.google.dev/gemini-api/docs/changelog
- Available regions: https://ai.google.dev/gemini-api/docs/available-regions
- Zero data retention: https://ai.google.dev/gemini-api/docs/zdr
- Interactions API overview (Beta note): https://ai.google.dev/gemini-api/docs/interactions-overview
- Gemini API Additional Terms of Service (effective 2026-03-23): https://ai.google.dev/gemini-api/terms
- Launch post, 2026-09-23 (consent verification, SynthID, C2PA, regional footnote): https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-8-text-to-speech/

Google policies
- Google APIs Terms of Service (modified 2021-11-09): https://developers.google.com/terms
- Google Terms of Service (effective 2026-07-30): https://policies.google.com/terms
- Generative AI Prohibited Use Policy (modified 2024-12-17): https://policies.google.com/terms/generative-ai/use-policy
- AI Studio robots.txt: https://aistudio.google.com/robots.txt

Google Cloud
- Chirp 3 Instant Custom Voice (updated 2026-10-13): https://docs.cloud.google.com/text-to-speech/docs/chirp3-instant-custom-voice
- Gemini-TTS in Cloud TTS (note on 3.8 availability): https://docs.cloud.google.com/text-to-speech/docs/gemini-tts
- Cloud TTS pricing: https://cloud.google.com/text-to-speech/pricing
- Cloud TTS release notes: https://docs.cloud.google.com/text-to-speech/docs/release-notes
- Chirp 3 speech-to-text (languages, diarization list): https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3

Secondary (not authoritative)
- Reviews quoting FLEURS WER for Gemini 3.5 Transcribe (multilingual, not Russian): https://eesel.ai/blog/gemini-3-5-transcribe-review
- Regional-block restatement: https://latenode.com/news/google-launches-gemini-3-8-tts-with-consent-gated-voice-cloning

## 11. Corrections and implementation notes (2026-10-19)

Checked against the live Google docs (fetched as `.md.txt`), the REST reference
and the installed `google-genai` SDK (2.25.0 on Python 3.11 and 2.31.0 on 3.12 and
3.13) while implementing `aistudio_speech.py`.

Facts corrected above:

- **Interactions API is GA, not Beta.** The overview says GA as of June 2026 and
  recommended for new projects; `generateContent` is legacy but fully supported.
  The earlier "avoid for stable production" claim did not survive the check. Only
  the Voices reference page still says Beta.
- **STT over generateContent** is not in the transcribe guide (Interactions only)
  but is in the REST reference and the SDK types; field names are given in 2.2.
- **Interactions stores by default** (`store=true`, 55 days paid / 1 day free).
  gaia always sends `store=false`.
- **Optional `speechConfig.languageCode`** exists for TTS on generateContent.
- **MP3/Opus output** appear in the generateContent REST enum only.

Confirmed unchanged: model names, prices (Transcribe about $0.005/min blended;
3.8 TTS $9 and $6 per 1M audio tokens, doubling on 2027-01-01), limits (1 h / 30
min with diarization or word timestamps; 8,192 in / 16,384 out TTS tokens), the 30
consent statements (copied verbatim into `aistudio/voices.py`; a fixture compares
them byte for byte), `store` default conflict (guide: stored by default; API
reference and SDK docstring: `false`), region list (Illinois, Texas, EEA, UK,
Switzerland, India for replication in AI Studio; Russia and Belarus not in the
available-regions list).

SDK facts relevant to the design:

- `client.voices` has `create`, `list` (`type_=`, `contexts=`), `get(id=)`,
  `delete(id=)`; bytes in SDK models are re-serialized as URL-safe base64, so
  responses must be decoded with an alphabet-tolerant decoder.
- The SDK's Voices/Interactions client retries at least once on 408/409/429/5xx
  and connection errors even when retries are configured off. `voices.create` is
  not idempotent, so gaia sends it as one raw REST POST.
- With `GOOGLE_GENAI_USE_VERTEXAI` or `GOOGLE_API_KEY` in the environment the SDK
  would change mode or key; gaia passes the key explicitly and forces Vertex off.

Design differences from sections 7 and 8, chosen by the lead's hard rules:

- No `--allow-in-repo` override and no `output/` fallback: inputs and outputs must
  be absolute paths outside the repo (and outside the super-project that embeds
  it); `--out-dir` is mandatory; test fixtures are built in temp directories.
- Free-tier gate: no API reveals the tier, so `voices replicate` and `stt`
  require `GAIA_AISTUDIO_PAID_PROJECT=1` (env or `aistudio.env`) or a per-run
  `--accept-unpaid-terms`.
- Key file: mode must be 0600 (0400 also accepted); a key file inside the repo is
  refused.
- Live tests use `GAIA_LIVE=1` (not `GAIA_LIVE_TESTS`) and a key; the audition
  clip after replication is opt-in through `--audition-out-dir`.
- `stt-live` (phase 3) is not implemented.
