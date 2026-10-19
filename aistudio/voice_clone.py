"""
Voice replication (cloning YOUR OWN voice) with guard rails.

Google's flow (https://ai.google.dev/gemini-api/docs/voice-replication):
  POST /v1beta/voices  {"store": bool, "voice": {"model", "type": "replicated",
  "display_name"?, "replicated": {"source_audio": {...}, "consent_audio": {...}}}}
Two recordings by the SAME adult speaker are required: a 10-30 s reference and
the fixed consent statement for the chosen language, read verbatim. Google
verifies that both come from the same speaker before the voice is created.

Guard rails added here (research doc, section 7.5):
  1. the exact consent statement for --lang is printed first;
  2. the caller must pass --i-am-the-speaker (and type a confirmation on a TTY);
  3. both clips are checked with ffprobe/ffmpeg and converted to 24 kHz mono
     16-bit WAV in a temp dir outside the repo (removed afterwards);
  4. `store` is always passed explicitly (default false: a 7 day voicekey);
  5. the alias is checked and the registry directory is writable BEFORE the API
     call, so a created voice can always be recorded locally;
  6. an append-only consent log keeps SHA-256 hashes (no audio, no transcript).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import ApiError, AudioError, UsageError
from . import audio, privacy
from .client import GeminiClient, pick
from .voices import (
    KEY_TTL,
    MODEL_FLASH_LITE,
    Registry,
    VoiceEntry,
    consent_statement,
    consent_text_block,
    iso,
    normalize_lang,
    resolve_model,
    utcnow,
)

REFERENCE_MIN_S, REFERENCE_MAX_S = 10.0, 30.0
REFERENCE_TOLERANCE_S = 0.5
CONSENT_MIN_S, CONSENT_MAX_S = 1.5, 30.0
CONSENT_TYPICAL_MAX_S = 15.0
LEVEL_MISMATCH_DB = 10.0
CONFIRM_PHRASE = "I am the speaker"

AUDITION_SENTENCES = {
    "ru": "Здравствуйте! Это короткая проверка синтезированного голоса.",
}
AUDITION_DEFAULT = "Hello! This is a short check of a synthesized voice."


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClipReport:
    info: audio.AudioInfo
    levels: audio.LevelStats


@dataclass
class Preflight:
    reference: ClipReport
    consent: ClipReport
    warnings: List[str] = field(default_factory=list)


def check_clips(ref: ClipReport, consent: ClipReport) -> Preflight:
    """Pure rule checks on probed clips. Raises AudioError on hard failures."""
    warnings: List[str] = []
    d = ref.info.duration_s
    if d < REFERENCE_MIN_S - REFERENCE_TOLERANCE_S or d > REFERENCE_MAX_S + REFERENCE_TOLERANCE_S:
        raise AudioError(
            f"reference clip is {d:.1f} s; Google requires {REFERENCE_MIN_S:.0f}-{REFERENCE_MAX_S:.0f} s of clean speech"
        )
    if d < REFERENCE_MIN_S or d > REFERENCE_MAX_S:
        warnings.append(f"reference clip is {d:.1f} s, slightly outside the documented 10-30 s window")
    c = consent.info.duration_s
    if c < CONSENT_MIN_S or c > CONSENT_MAX_S:
        raise AudioError(f"consent clip is {c:.1f} s; it should contain only the spoken statement (a few seconds)")
    if c > CONSENT_TYPICAL_MAX_S:
        warnings.append(f"consent clip is {c:.1f} s, longer than expected for one sentence; trim silence and extras")
    for name, clip in (("reference", ref), ("consent", consent)):
        if clip.info.channels and clip.info.channels > 1:
            warnings.append(f"{name} clip has {clip.info.channels} channels; it will be downmixed to mono")
        if clip.levels.max_db is not None and clip.levels.max_db >= -0.1:
            warnings.append(f"{name} clip peaks at {clip.levels.max_db:.1f} dB: possible clipping")
        if clip.levels.mean_db is not None and clip.levels.mean_db < -40.0:
            warnings.append(f"{name} clip is very quiet (mean {clip.levels.mean_db:.1f} dB)")
    if ref.levels.mean_db is not None and consent.levels.mean_db is not None:
        if abs(ref.levels.mean_db - consent.levels.mean_db) > LEVEL_MISMATCH_DB:
            warnings.append(
                "the two clips differ a lot in level (>10 dB): were they recorded with the same microphone, "
                "gain and room? Google rejects voices when the speakers do not match."
            )
    return Preflight(ref, consent, warnings)


def preflight(ref_path: Path, consent_path: Path) -> Preflight:
    return check_clips(
        ClipReport(audio.probe(ref_path), audio.level_stats(ref_path)),
        ClipReport(audio.probe(consent_path), audio.level_stats(consent_path)),
    )


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
def build_create_request(
    model: str,
    store: bool,
    source_b64: str,
    consent_b64: str,
    display_name: Optional[str] = None,
    mime_type: str = "audio/wav",
) -> dict:
    """Exactly the documented CreateVoice body. `store` is always present."""
    voice = {
        "model": model,
        "type": "replicated",
        "replicated": {
            "source_audio": {"mime_type": mime_type, "data": source_b64},
            "consent_audio": {"mime_type": mime_type, "data": consent_b64},
        },
    }
    if display_name and store:
        voice["display_name"] = display_name  # only stored voices keep metadata
    body = {"store": bool(store), "voice": voice}
    return body


def parse_create_response(resp: dict, store: bool) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """Return (voice_id, voice_key, expire_time) and check the expected one is there."""
    voice_id = pick(resp, "id")
    voice_key = pick(resp, "key")
    expire = pick(resp, "expire_time", "expireTime")
    if store and not voice_id:
        raise ApiError("voices.create returned no voice id for store=true")
    if not store and not voice_key:
        raise ApiError("voices.create returned no voice key for store=false")
    return voice_id, voice_key, expire


# --------------------------------------------------------------------------- #
# Consent log
# --------------------------------------------------------------------------- #
def consent_record(
    *,
    alias: str,
    lang: str,
    model: str,
    store: bool,
    reference_sha256: str,
    consent_sha256: str,
    voice_id: Optional[str],
    voice_key: Optional[str],
    timestamp: str,
) -> dict:
    """One log line. Hashes only: no audio, no transcript, no key (a stateless
    key is represented by its SHA-256)."""
    return {
        "ts": timestamp,
        "alias": alias,
        "language": lang,
        "statement_sha256": hashlib.sha256(consent_statement(lang).encode("utf-8")).hexdigest(),
        "consent_audio_sha256": consent_sha256,
        "reference_audio_sha256": reference_sha256,
        "voice_id": voice_id,
        "voice_key_sha256": hashlib.sha256(voice_key.encode("utf-8")).hexdigest() if voice_key else None,
        "model": model,
        "store": store,
    }


def append_consent_log(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class ReplicateResult:
    entry: Optional[VoiceEntry]
    request: dict  # printable (placeholders instead of audio)
    preflight: Preflight
    dry_run: bool


Progress = Callable[[str], None]


def require_speaker_flag(i_am_the_speaker: bool) -> None:
    if not i_am_the_speaker:
        raise UsageError(
            "refusing to replicate a voice without --i-am-the-speaker. Only clone your own voice, "
            "using your own consent recording."
        )


def audition_sentence(lang: str) -> str:
    return AUDITION_SENTENCES.get(lang.split("-")[0].lower(), AUDITION_DEFAULT)


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def replicate(
    client: Optional[GeminiClient],
    registry: Registry,
    consent_log: Path,
    *,
    reference: str,
    consent: str,
    lang: str,
    alias: str,
    store: bool,
    model: str = MODEL_FLASH_LITE,
    i_am_the_speaker: bool,
    confirm: Optional[Callable[[str], bool]] = None,
    dry_run: bool = False,
    progress: Optional[Progress] = None,
) -> ReplicateResult:
    say = progress or (lambda _m: None)
    require_speaker_flag(i_am_the_speaker)
    lang = normalize_lang(lang)
    model = resolve_model(model)
    registry.check_alias_free(alias)
    ref_path = privacy.private_input(reference, "--reference")
    consent_path = privacy.private_input(consent, "--consent")
    if ref_path == consent_path:
        raise UsageError("--reference and --consent must be two different recordings")

    say(consent_text_block(lang))
    pre = preflight(ref_path, consent_path)
    for w in pre.warnings:
        say(f"warning: {w}")
    say(
        f"reference {pre.reference.info.duration_s:.1f} s, consent {pre.consent.info.duration_s:.1f} s, "
        f"model {model}, store={str(store).lower()}"
        + ("" if store else " (stateless voicekey, valid 7 days)")
    )

    if dry_run:
        est = lambda i: int(24000 * 2 * i.duration_s) + 44  # noqa: E731 - size of the converted WAV
        req = build_create_request(
            model, store,
            f"<inline audio: {est(pre.reference.info)} bytes>",
            f"<inline audio: {est(pre.consent.info)} bytes>",
            display_name=alias,
        )
        return ReplicateResult(None, req, pre, True)

    if confirm is not None and not confirm(CONFIRM_PHRASE):
        raise UsageError("confirmation phrase did not match; nothing was sent")
    if client is None:
        raise UsageError("internal: API client required")
    try:  # fail BEFORE the API call if the voice could not be recorded afterwards
        registry.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise UsageError(f"cannot create {registry.path.parent}") from None
    if not os.access(registry.path.parent, os.W_OK):
        raise UsageError(f"cannot write to {registry.path.parent}; fix its permissions first")

    tmp = audio.make_temp_dir()
    try:
        ref_wav = audio.to_wav_24k_mono(ref_path, tmp, "reference")
        consent_wav = audio.to_wav_24k_mono(consent_path, tmp, "consent")
        body = build_create_request(model, store, _b64(ref_wav), _b64(consent_wav), display_name=alias)
        say("sending both clips to Google for speaker verification and voice creation")
        resp = client.create_voice(body)
    finally:
        audio.remove_temp_dir(tmp)

    voice_id, voice_key, expire = parse_create_response(resp, store)
    now = utcnow()
    expires = expire or (iso(now + KEY_TTL) if not store else None)
    entry = VoiceEntry(
        alias=alias, model=model, language_code=lang, store=store, created=iso(now),
        expires=expires, voice_id=voice_id if store else None, voice_key=None if store else voice_key,
    )
    registry.add(entry)
    append_consent_log(
        consent_log,
        consent_record(
            alias=alias, lang=lang, model=model, store=store,
            reference_sha256=privacy.sha256_file(ref_path), consent_sha256=privacy.sha256_file(consent_path),
            voice_id=voice_id, voice_key=voice_key, timestamp=iso(now),
        ),
    )
    say(f"voice created and saved as alias {alias!r}" + (f" (id {voice_id})" if voice_id else ""))
    if expires:
        say(f"expires: {expires}")
    placeholder = build_create_request(model, store, "<inline audio>", "<inline audio>", display_name=alias)
    return ReplicateResult(entry, placeholder, pre, False)
