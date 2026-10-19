"""
Text-to-speech with Gemini 3.8 TTS (`gemini-3.8-flash-tts`,
`gemini-3.8-flash-lite-tts`).

Request builders return the documented REST shape (see client.Request); the
parsers read the documented response shapes. Long scripts are chunked and the
WAV chunks are stitched locally. Custom (replicated/designed) voices cannot be
mixed in one multi-speaker request, so dialogues are synthesized turn by turn.

Docs: https://ai.google.dev/gemini-api/docs/speech-generation
      https://ai.google.dev/api/generate-content (SpeechConfig, SpeechMetadata)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence

from . import ApiError, UsageError
from .audio import chunk_text, concat_wavs, l16_to_wav, wav_duration
from .client import API_GENERATE_CONTENT, API_INTERACTIONS, GeminiClient, Request, decode_b64, pick
from .voices import (
    MODEL_FLASH,
    MODEL_FLASH_LITE,
    Registry,
    ResolvedVoice,
    resolve_model,
    resolve_voice,
)

DEFAULT_MODEL = "flash-lite"
DEFAULT_MAX_CHARS = 2500  # ~3 min of speech; the hard cap is 16,384 output tokens (~10.9 min)
DEFAULT_GAP_MS = 250
AUDIO_TOKENS_PER_SECOND = 25
CHARS_PER_SECOND = 14.0  # rough speaking rate used only for cost estimates

# Paid-tier standard output price, USD per second of audio (pricing page,
# fetched 2026-10-19): {model: (through 2026-12-31, from 2027-01-01)}.
PRICE_PER_10S = {
    MODEL_FLASH: (0.00225, 0.0045),
    MODEL_FLASH_LITE: (0.0015, 0.003),
}
PRICE_CHANGE = date(2027, 1, 1)

OUTPUT_FORMATS = ("wav", "mp3", "flac", "ogg", "l16")


# --------------------------------------------------------------------------- #
# Request builders
# --------------------------------------------------------------------------- #
def build_speech_request(
    model: str,
    text: str,
    voice: str,
    style: Optional[str] = None,
    lang: Optional[str] = None,
    api: str = API_GENERATE_CONTENT,
) -> Request:
    """One single-speaker request. `voice` is a prebuilt name, Voice Library
    speaker, `voice_...` id or `voicekey_...` key. Text is read verbatim, so
    delivery directions belong in `style`; inline tags such as <short pause>
    stay in English."""
    if not text.strip():
        raise UsageError("nothing to synthesize: the text is empty")
    if api == API_GENERATE_CONTENT:
        part: Dict[str, object] = {"text": text}
        if style:
            part["speechMetadata"] = {"style": style}
        speech: Dict[str, object] = {"voiceConfig": {"voice": voice}}
        if lang:
            speech["languageCode"] = lang
        body = {
            "contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech},
        }
        return Request(api, model, body)
    if api == API_INTERACTIONS:
        if lang:
            raise UsageError("--lang is only supported with --api generate-content")
        item: Dict[str, object] = {"type": "text", "text": text}
        if style:
            item["annotations"] = [{"type": "speech_metadata", "style": style}]
        body = {
            "model": model,
            "input": [{"type": "user_input", "content": [item]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice}]},
            "store": False,
        }
        return Request(api, model, body)
    raise ValueError(f"unknown api {api!r}")


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def _no_audio(resp: dict) -> ApiError:
    reasons = []
    for cand in resp.get("candidates", []) or []:
        fr = pick(cand, "finishReason", "finish_reason")
        if fr:
            reasons.append(str(fr))
    block = pick(pick(resp, "promptFeedback", "prompt_feedback", default={}), "blockReason", "block_reason")
    if block:
        reasons.append(f"blocked: {block}")
    status = resp.get("status")
    if status and status != "completed":
        reasons.append(f"status {status}")
    detail = ", ".join(reasons) if reasons else "no candidates"
    return ApiError(f"the API returned no audio ({detail})")


def extract_audio(api: str, resp: dict) -> "tuple[bytes, str]":
    """Return (raw bytes, mime type) of the generated audio."""
    found: Optional["tuple[str, str]"] = None
    if api == API_GENERATE_CONTENT:
        for cand in resp.get("candidates", []) or []:
            for part in (pick(cand, "content", default={}) or {}).get("parts", []) or []:
                blob = pick(part, "inlineData", "inline_data")
                mime = str(pick(blob, "mimeType", "mime_type", default="") or "")
                if blob and pick(blob, "data") and mime.lower().startswith("audio/"):
                    found = (blob["data"], mime)
    else:
        for step in resp.get("steps", []) or []:
            if step.get("type") != "model_output":
                continue
            for content in step.get("content", []) or []:
                if content.get("type") == "audio" and content.get("data"):
                    found = (content["data"], str(content.get("mime_type") or "audio/wav"))
    if not found:
        raise _no_audio(resp)
    return decode_b64(found[0]), found[1]


def to_wav(raw: bytes, mime: str) -> bytes:
    """Normalize what the API returned to a RIFF WAV."""
    if raw[:4] == b"RIFF":
        return raw
    low = mime.lower()
    if "l16" in low or "pcm" in low:
        m = re.search(r"rate=(\d+)", low)
        return l16_to_wav(raw, int(m.group(1)) if m else 24000)
    raise ApiError(f"unexpected audio type from the API: {mime}")


# --------------------------------------------------------------------------- #
# Cost estimate (dry-run)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Estimate:
    seconds: float
    usd_now: float
    usd_2027: float


def estimate_cost(chars: int, model: str) -> Optional[Estimate]:
    prices = PRICE_PER_10S.get(model)
    if not prices:
        return None
    seconds = chars / CHARS_PER_SECOND
    return Estimate(seconds, seconds / 10 * prices[0], seconds / 10 * prices[1])


def describe_estimate(chars: int, model: str) -> str:
    est = estimate_cost(chars, model)
    if not est:
        return "cost: unknown for this model"
    return (
        f"estimate: ~{est.seconds:.0f} s of audio, ~${est.usd_now:.4f} at list price "
        f"(paid tier; ~${est.usd_2027:.4f} from {PRICE_CHANGE.isoformat()}). Rough: assumes {CHARS_PER_SECOND:.0f} chars/s."
    )


# --------------------------------------------------------------------------- #
# Dialogue turns
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str


_TURN_RE = re.compile(r"^([^:\n]{1,40}):\s*(.*)$")


def parse_turns(text: str, speakers: Sequence[str]) -> List[Turn]:
    """Parse 'NAME: line' scripts. Lines that do not start with a known
    speaker label continue the previous turn."""
    known = set(speakers)
    turns: List[List[str]] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _TURN_RE.match(line)
        if m and m.group(1).strip() in known:
            turns.append([m.group(1).strip(), m.group(2).strip()])
        elif turns:
            turns[-1][1] = (turns[-1][1] + " " + line).strip()
        else:
            raise UsageError(
                "the script must start with a 'NAME: text' line using a speaker defined with --speaker NAME=VOICE"
            )
    out = [Turn(s, t) for s, t in turns if t]
    if not out:
        raise UsageError("no turns found in the script")
    return out


def parse_speaker_args(items: Sequence[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for item in items:
        name, sep, voice = item.partition("=")
        name, voice = name.strip(), voice.strip()
        if not sep or not name or not voice:
            raise UsageError(f"--speaker expects NAME=VOICE, got {item!r}")
        if name in mapping:
            raise UsageError(f"speaker {name!r} defined twice")
        mapping[name] = voice
    return mapping


# --------------------------------------------------------------------------- #
# Synthesis driver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SynthResult:
    wav: bytes
    requests: int
    stitched: bool  # True if the WAV was assembled locally (provenance metadata may not survive)
    duration_s: float


Progress = Callable[[str], None]


def _synth_chunks(
    client: GeminiClient,
    model: str,
    chunks: Sequence[str],
    voice: str,
    style: Optional[str],
    lang: Optional[str],
    progress: Optional[Progress],
    label: str,
) -> List[bytes]:
    wavs: List[bytes] = []
    for i, chunk in enumerate(chunks, 1):
        req = build_speech_request(model, chunk, voice, style=style, lang=lang, api=client.api)
        if progress:
            progress(f"{label}chunk {i}/{len(chunks)}: {len(chunk)} chars")
        resp = client.generate(req.model, req.body, "tts")
        raw, mime = extract_audio(client.api, resp)
        wavs.append(to_wav(raw, mime))
    return wavs


def synthesize(
    client: GeminiClient,
    text: str,
    voice: ResolvedVoice,
    *,
    model: str = DEFAULT_MODEL,
    style: Optional[str] = None,
    lang: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    gap_ms: int = DEFAULT_GAP_MS,
    progress: Optional[Progress] = None,
) -> SynthResult:
    model = resolve_model(model)
    chunks = chunk_text(text, max_chars)
    if not chunks:
        raise UsageError("nothing to synthesize: the text is empty")
    wavs = _synth_chunks(client, model, chunks, voice.value, style, lang, progress, "")
    if len(wavs) == 1:
        return SynthResult(wavs[0], 1, False, wav_duration(wavs[0]))
    merged = concat_wavs(wavs, gap_ms)
    return SynthResult(merged, len(wavs), True, wav_duration(merged))


def synthesize_dialogue(
    client: GeminiClient,
    turns: Sequence[Turn],
    voices: Dict[str, ResolvedVoice],
    *,
    model: str = DEFAULT_MODEL,
    style: Optional[str] = None,
    lang: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    gap_ms: int = DEFAULT_GAP_MS,
    progress: Optional[Progress] = None,
) -> SynthResult:
    """Turn-by-turn synthesis (works with prebuilt and custom voices alike),
    stitched with a short gap."""
    model = resolve_model(model)
    wavs: List[bytes] = []
    total = len(turns)
    for n, turn in enumerate(turns, 1):
        voice = voices[turn.speaker]
        chunks = chunk_text(turn.text, max_chars)
        wavs += _synth_chunks(client, model, chunks, voice.value, style, lang, progress, f"turn {n}/{total}, ")
    merged = concat_wavs(wavs, gap_ms)
    return SynthResult(merged, len(wavs), True, wav_duration(merged))


def resolve_speakers(mapping: Dict[str, str], registry: Optional[Registry]) -> Dict[str, ResolvedVoice]:
    return {name: resolve_voice(spec, registry) for name, spec in mapping.items()}
