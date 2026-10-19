"""
Speech-to-text with Gemini 3.5 Transcribe (`gemini-3.5-transcribe`):
optional language hint, speaker diarization, word timestamps, smart mode,
custom vocabulary.

The unary model is documented for the Interactions API
(https://ai.google.dev/gemini-api/docs/transcribe) and exposed on generateContent through
`generationConfig.audioTranscriptionConfig` and `Part.audioTranscription`
(https://ai.google.dev/api/generate-content). Both request shapes and both
response shapes are implemented; the choice is made in client.py.

Privacy: no transcript text is ever logged (only counts and durations); audio
goes inline when small, otherwise through the Files API and the uploaded object
is deleted in a `finally` block.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import ApiError, AudioError, UsageError
from .audio import AudioInfo, convert, probe
from .client import API_GENERATE_CONTENT, API_INTERACTIONS, GeminiClient, Request, UploadedFile, parse_offset, pick
from .voices import MODEL_TRANSCRIBE

MAX_SECONDS = 3600.0  # 1 h per request
MAX_SECONDS_WITH_FEATURES = 1800.0  # 30 min when diarization or word timestamps are on
MAX_VOCABULARY = 1000
INLINE_MAX_BYTES = 14 * 1024 * 1024  # base64 inflates by 4/3; the request cap is 20 MB in total
TRANSCRIPT_FORMATS = ("txt", "json", "srt", "vtt")

# Extension -> MIME for the formats the model accepts (transcribe guide,
# "Supported audio formats"). Raw L16/A-law/mu-law need a rate and are converted instead.
MIME_BY_EXT = {
    ".wav": "audio/wav", ".mp3": "audio/mp3", ".aiff": "audio/aiff", ".aif": "audio/aiff",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac", ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg", ".m4a": "audio/m4a", ".opus": "audio/opus", ".webm": "audio/webm",
}


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SttOptions:
    language_codes: Sequence[str] = ()
    diarize: bool = False
    words: bool = False
    smart: bool = False
    vocabulary: Sequence[str] = ()

    @property
    def features(self) -> bool:
        return self.diarize or self.words


def validate_options(opts: SttOptions) -> None:
    """Reject locally what the API rejects (saves a round trip and the upload)."""
    if opts.vocabulary and opts.features:
        raise UsageError("custom vocabulary cannot be combined with --diarize or --words")
    if opts.smart and opts.features:
        raise UsageError("--smart cannot be combined with --diarize or --words")
    if len(opts.vocabulary) > MAX_VOCABULARY:
        raise UsageError(f"custom vocabulary is limited to {MAX_VOCABULARY} terms")


# --------------------------------------------------------------------------- #
# Request builders
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AudioPart:
    mime_type: str
    data_b64: Optional[str] = None  # inline
    uri: Optional[str] = None  # Files API
    inline_bytes: int = 0  # used only to describe a dry-run placeholder

    def inline_data(self) -> str:
        return self.data_b64 if self.data_b64 is not None else f"<inline audio: {self.inline_bytes} bytes>"


def build_transcribe_request(
    audio: AudioPart, opts: SttOptions, model: str = MODEL_TRANSCRIBE, api: str = API_GENERATE_CONTENT
) -> Request:
    validate_options(opts)
    if not audio.uri and audio.data_b64 is None and not audio.inline_bytes:
        raise ValueError("AudioPart needs inline data or a file uri")
    codes = list(opts.language_codes)
    if api == API_GENERATE_CONTENT:
        if audio.uri:
            part: Dict[str, object] = {"fileData": {"mimeType": audio.mime_type, "fileUri": audio.uri}}
        else:
            part = {"inlineData": {"mimeType": audio.mime_type, "data": audio.inline_data()}}
        cfg: Dict[str, object] = {"languageCodes": codes}
        if opts.vocabulary:
            cfg["customVocabulary"] = list(opts.vocabulary)
        if opts.diarize:
            cfg["diarization"] = True
        if opts.words:
            cfg["wordTimestamp"] = True
        if opts.smart:
            cfg["mode"] = "SMART"
        body = {
            "contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {"audioTranscriptionConfig": cfg},
        }
        return Request(api, model, body)
    if api == API_INTERACTIONS:
        item: Dict[str, object] = {"type": "audio", "mime_type": audio.mime_type}
        if audio.uri:
            item["uri"] = audio.uri
        else:
            item["data"] = audio.inline_data()
        tcfg: Dict[str, object] = {"language_codes": codes}
        if opts.vocabulary:
            tcfg["custom_vocabulary"] = list(opts.vocabulary)
        if opts.smart:
            tcfg["mode"] = "smart"
        elif opts.features:
            mode: Dict[str, object] = {"type": "verbatim"}
            if opts.diarize:
                mode["diarization_mode"] = "speaker"
            if opts.words:
                mode["timestamp_granularities"] = ["word"]
            tcfg["mode"] = mode
        body = {
            "model": model,
            "input": [item],
            "generation_config": {"transcription_config": tcfg},
            "store": False,
        }
        return Request(api, model, body)
    raise ValueError(f"unknown api {api!r}")


# --------------------------------------------------------------------------- #
# Transcript model + parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Word:
    text: str
    start: Optional[float]
    end: Optional[float]
    speaker: Optional[str] = None


@dataclass(frozen=True)
class Segment:
    speaker: Optional[str]
    text: str
    start: Optional[float] = None
    end: Optional[float] = None


@dataclass
class Transcript:
    segments: List[Segment] = field(default_factory=list)
    words: List[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text).strip()

    @property
    def speakers(self) -> List[str]:
        seen: List[str] = []
        for s in self.segments:
            if s.speaker and s.speaker not in seen:
                seen.append(s.speaker)
        return seen


def _words_from(items) -> List[Word]:
    out: List[Word] = []
    for w in items or []:
        text = pick(w, "word", "text", default="")
        out.append(
            Word(
                text=str(text),
                start=parse_offset(pick(w, "startOffset", "start_offset")),
                end=parse_offset(pick(w, "endOffset", "end_offset")),
                speaker=pick(w, "speaker", "speakerLabel", "speaker_label"),
            )
        )
    return out


def segments_from_words(words: Sequence[Word]) -> List[Segment]:
    """Group consecutive words of the same speaker into segments."""
    segs: List[Segment] = []
    cur: List[Word] = []
    for w in words:
        if cur and w.speaker != cur[-1].speaker:
            segs.append(_segment(cur))
            cur = []
        cur.append(w)
    if cur:
        segs.append(_segment(cur))
    return segs


def _segment(words: Sequence[Word]) -> Segment:
    starts = [w.start for w in words if w.start is not None]
    ends = [w.end for w in words if w.end is not None]
    return Segment(
        speaker=words[0].speaker,
        text=" ".join(w.text for w in words),
        start=min(starts) if starts else None,
        end=max(ends) if ends else None,
    )


def parse_generate_content(resp: dict) -> Transcript:
    """candidates[].content.parts[] with Part.audioTranscription
    {text, speakerLabel, words[]}; falls back to plain text parts."""
    tr = Transcript()
    cands = resp.get("candidates") or []
    if not cands:
        raise ApiError("the API returned no transcript (no candidates)")
    for part in (pick(cands[0], "content", default={}) or {}).get("parts", []) or []:
        at = pick(part, "audioTranscription", "audio_transcription")
        if at:
            words = _words_from(at.get("words"))
            speaker = pick(at, "speakerLabel", "speaker_label")
            tr.words.extend(
                Word(w.text, w.start, w.end, w.speaker or speaker) for w in words
            )
            starts = [w.start for w in words if w.start is not None]
            ends = [w.end for w in words if w.end is not None]
            tr.segments.append(
                Segment(speaker, str(at.get("text", "")).strip(),
                        min(starts) if starts else None, max(ends) if ends else None)
            )
        elif part.get("text") and not part.get("thought"):
            tr.segments.append(Segment(None, str(part["text"]).strip()))
    if not tr.segments:
        raise ApiError("the API returned no transcript text")
    return tr


def parse_interactions(resp: dict) -> Transcript:
    """steps[].content[] text blocks with `word_info` annotations."""
    tr = Transcript()
    texts: List[str] = []
    for step in resp.get("steps", []) or []:
        if step.get("type") != "model_output":
            continue
        for content in step.get("content", []) or []:
            if content.get("type") != "text":
                continue
            texts.append(str(content.get("text", "")))
            for ann in content.get("annotations", []) or []:
                if ann.get("type") == "word_info":
                    tr.words.extend(_words_from([ann]))
    if not texts and not resp.get("output_text"):
        raise ApiError("the API returned no transcript text")
    diarized = any(w.speaker for w in tr.words)
    if tr.words and diarized:
        tr.segments = segments_from_words(tr.words)
    else:
        full = " ".join(t.strip() for t in texts).strip() or str(resp.get("output_text", "")).strip()
        starts = [w.start for w in tr.words if w.start is not None]
        ends = [w.end for w in tr.words if w.end is not None]
        tr.segments = [Segment(None, full, min(starts) if starts else None, max(ends) if ends else None)]
    return tr


def parse_response(api: str, resp: dict) -> Transcript:
    return parse_interactions(resp) if api == API_INTERACTIONS else parse_generate_content(resp)


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def make_cues(words: Sequence[Word], max_chars: int = 84, max_seconds: float = 6.0, gap_seconds: float = 1.0) -> List[Cue]:
    """Subtitle cues from word timestamps. Breaks on speaker change, on a pause
    longer than `gap_seconds`, and when a cue gets too long."""
    cues: List[Cue] = []
    cur: List[Word] = []

    def flush() -> None:
        if not cur:
            return
        text = " ".join(w.text for w in cur)
        if cur[0].speaker:
            text = f"[{cur[0].speaker}] {text}"
        cues.append(Cue(cur[0].start or 0.0, max(cur[-1].end or cur[-1].start or 0.0, cur[0].start or 0.0), text))
        cur.clear()

    for w in words:
        if w.start is None or w.end is None:
            continue
        if cur:
            gap = w.start - (cur[-1].end or w.start)
            text_len = sum(len(x.text) + 1 for x in cur)
            if (w.speaker != cur[0].speaker or gap > gap_seconds or text_len + len(w.text) > max_chars
                    or w.end - (cur[0].start or 0.0) > max_seconds):
                flush()
        cur.append(w)
    flush()
    return cues


def _ts(seconds: float, sep: str) -> str:
    ms = int(round(max(seconds, 0.0) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: Sequence[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_ts(c.start, ',')} --> {_ts(c.end, ',')}\n{c.text}\n")
    return "\n".join(out)


def to_vtt(cues: Sequence[Cue]) -> str:
    out = ["WEBVTT\n"]
    for c in cues:
        out.append(f"{_ts(c.start, '.')} --> {_ts(c.end, '.')}\n{c.text}\n")
    return "\n".join(out)


def to_txt(tr: Transcript) -> str:
    if tr.speakers:
        return "\n".join(f"{s.speaker or '?'}: {s.text}" for s in tr.segments) + "\n"
    return tr.text + "\n"


def to_json(tr: Transcript, meta: dict) -> str:
    def num(x: Optional[float]) -> Optional[float]:
        return None if x is None else round(x, 3)

    payload = {
        **meta,
        "text": tr.text,
        "speakers": tr.speakers,
        "segments": [
            {"speaker": s.speaker, "text": s.text, "start": num(s.start), "end": num(s.end)} for s in tr.segments
        ],
        "words": [
            {"text": w.text, "start": num(w.start), "end": num(w.end), "speaker": w.speaker} for w in tr.words
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render(fmt: str, tr: Transcript, meta: dict) -> str:
    if fmt == "txt":
        return to_txt(tr)
    if fmt == "json":
        return to_json(tr, meta)
    if fmt in ("srt", "vtt"):
        if not any(w.start is not None for w in tr.words):
            raise UsageError(f"--format {fmt} needs word timestamps: add --words")
        cues = make_cues(tr.words)
        return to_srt(cues) if fmt == "srt" else to_vtt(cues)
    raise UsageError(f"unknown transcript format {fmt!r}")


# --------------------------------------------------------------------------- #
# Audio preparation and the driver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    mime_type: str
    info: AudioInfo
    converted: bool


def check_duration(info: AudioInfo, opts: SttOptions) -> None:
    limit = MAX_SECONDS_WITH_FEATURES if opts.features else MAX_SECONDS
    if info.duration_s > limit:
        which = "with --diarize/--words " if opts.features else ""
        raise AudioError(
            f"audio is {info.duration_s / 60:.1f} min; the model accepts at most {limit / 60:.0f} min per request "
            f"{which}(diarization labels are not comparable across separate requests, so gaia does not split). "
            "Trim the file first."
        )


def prepare_audio(path: Path, opts: SttOptions, tmp_dir: Path) -> PreparedAudio:
    info = probe(path)
    check_duration(info, opts)
    mime = MIME_BY_EXT.get(path.suffix.lower())
    if mime:
        return PreparedAudio(path, mime, info, False)
    # Unsupported container/extension: lossless 16 kHz mono FLAC (speech quality, small).
    out = convert(path, tmp_dir / "stt-input.flac", rate=16000, channels=1, codec="flac")
    return PreparedAudio(out, "audio/flac", probe(out), True)


Progress = Callable[[str], None]


def transcribe_file(
    client: GeminiClient,
    prepared: PreparedAudio,
    opts: SttOptions,
    model: str = MODEL_TRANSCRIBE,
    progress: Optional[Progress] = None,
) -> "tuple[Transcript, dict]":
    """Returns (transcript, meta). Meta has counts/durations only."""
    size = prepared.path.stat().st_size
    uploaded: Optional[UploadedFile] = None
    meta = {
        "model": model,
        "api": client.api,
        "duration_s": round(prepared.info.duration_s, 3),
        "language_codes": list(opts.language_codes),
        "diarization": opts.diarize,
        "word_timestamps": opts.words,
        "smart": opts.smart,
    }
    try:
        if size <= INLINE_MAX_BYTES:
            audio = AudioPart(prepared.mime_type, data_b64=base64.b64encode(prepared.path.read_bytes()).decode("ascii"))
            meta["transport"] = "inline"
        else:
            if progress:
                progress(f"uploading {size / 1e6:.1f} MB through the Files API (deleted afterwards)")
            uploaded = client.upload_file(
                str(prepared.path), prepared.mime_type, display_name=f"gaia-stt-{size}"
            )
            audio = AudioPart(uploaded.mime_type, uri=uploaded.uri)
            meta["transport"] = "files-api"
        req = build_transcribe_request(audio, opts, model, client.api)
        resp = client.generate(req.model, req.body, "stt")
        return parse_response(client.api, resp), meta
    finally:
        if uploaded is not None:
            ok = client.delete_file(uploaded.name)
            if progress:
                progress("uploaded file deleted" if ok else "WARNING: could not delete the uploaded file; it expires after 48 h")
