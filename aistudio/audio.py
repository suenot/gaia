"""
Audio helpers: ffprobe/ffmpeg preflight and conversion, WAV (RIFF) handling,
PCM concatenation, and text chunking for long TTS scripts.

Everything here works on local files/bytes only. Temporary files go to a fresh
temp directory under the system temp location (never the repo) which callers
remove in a `finally` block.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from . import AudioError, privacy

TTS_SAMPLE_RATE = 24000  # Gemini TTS output / voice-replication input rate

# --------------------------------------------------------------------------- #
# ffprobe / ffmpeg
# --------------------------------------------------------------------------- #
def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AudioError(f"{name} not found on PATH (macOS: brew install ffmpeg)")
    return path


def _child_env() -> dict:
    """Environment for ffmpeg/ffprobe: the API key is not theirs to see."""
    return {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}


def _run(cmd: Sequence[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        raise AudioError(f"{Path(cmd[0]).name} timed out after {timeout}s") from None
    except OSError as exc:
        raise AudioError(f"cannot run {Path(cmd[0]).name}: {exc.strerror}") from None


@dataclass(frozen=True)
class AudioInfo:
    duration_s: float
    sample_rate: Optional[int]
    channels: Optional[int]
    codec: Optional[str]
    sample_fmt: Optional[str]
    bit_depth: Optional[int]
    size_bytes: int
    format_name: Optional[str]

    @property
    def is_wav_24k_mono_16(self) -> bool:
        return (
            self.codec == "pcm_s16le"
            and self.sample_rate == TTS_SAMPLE_RATE
            and self.channels == 1
            and (self.format_name or "").startswith("wav")
        )


def parse_ffprobe(payload: dict, size_bytes: int) -> AudioInfo:
    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise AudioError("no audio stream found in the file")
    stream = streams[0]
    fmt = payload.get("format", {})

    def num(value, cast):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    duration = num(stream.get("duration"), float) or num(fmt.get("duration"), float)
    if not duration or duration <= 0:
        raise AudioError("cannot determine the audio duration")
    bits = num(stream.get("bits_per_sample"), int) or num(stream.get("bits_per_raw_sample"), int) or None
    return AudioInfo(
        duration_s=duration,
        sample_rate=num(stream.get("sample_rate"), int),
        channels=num(stream.get("channels"), int),
        codec=stream.get("codec_name"),
        sample_fmt=stream.get("sample_fmt"),
        bit_depth=bits,
        size_bytes=size_bytes,
        format_name=fmt.get("format_name"),
    )


def probe(path: Path) -> AudioInfo:
    cmd = [
        _tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
    ]
    res = _run(cmd, timeout=60)
    if res.returncode != 0:
        raise AudioError("ffprobe could not read the file (not audio, or corrupt)")
    try:
        payload = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        raise AudioError("ffprobe returned unreadable output") from None
    return parse_ffprobe(payload, path.stat().st_size)


@dataclass(frozen=True)
class LevelStats:
    mean_db: Optional[float]
    max_db: Optional[float]


_VOL_RE = re.compile(r"(mean_volume|max_volume):\s*(-?[0-9.]+|-inf)\s*dB")


def parse_volumedetect(stderr: str) -> LevelStats:
    found = {}
    for key, val in _VOL_RE.findall(stderr or ""):
        found[key] = float("-inf") if val == "-inf" else float(val)
    return LevelStats(mean_db=found.get("mean_volume"), max_db=found.get("max_volume"))


def level_stats(path: Path) -> LevelStats:
    cmd = [
        _tool("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-",
    ]
    res = _run(cmd, timeout=300)
    if res.returncode != 0:
        raise AudioError("ffmpeg could not analyze the levels")
    return parse_volumedetect(res.stderr)


def make_temp_dir() -> Path:
    """A private (0700) temp directory outside the repository tree."""
    tmp = Path(tempfile.mkdtemp(prefix="gaia-aistudio-")).resolve()
    if privacy.is_inside(tmp, privacy.forbidden_roots()):
        shutil.rmtree(tmp, ignore_errors=True)
        raise AudioError("TMPDIR points inside the repository tree; set TMPDIR elsewhere")
    return tmp


def remove_temp_dir(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def convert(src: Path, dst: Path, *, rate: int, channels: int = 1, codec: str = "pcm_s16le") -> Path:
    """Re-encode `src` to `dst` (format chosen by the extension)."""
    cmd = [
        _tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn",
        "-ac", str(channels), "-ar", str(rate), "-c:a", codec, str(dst),
    ]
    res = _run(cmd)
    if res.returncode != 0 or not dst.exists():
        raise AudioError("ffmpeg conversion failed")
    return dst


def to_wav_24k_mono(src: Path, tmp_dir: Path, name: str) -> Path:
    """24 kHz mono 16-bit PCM WAV (recommended input for voice replication).
    Returns `src` untouched when it already conforms."""
    info = probe(src)
    if info.is_wav_24k_mono_16:
        return src
    return convert(src, tmp_dir / f"{name}.wav", rate=TTS_SAMPLE_RATE)


def encode_output(wav_path: Path, dst: Path, fmt: str) -> Path:
    """Transcode a WAV file for delivery: mp3, flac, ogg (Opus-in-Ogg uses
    libopus; falls back to Vorbis name `ogg`)."""
    codecs = {"mp3": "libmp3lame", "flac": "flac", "ogg": "libvorbis"}
    if fmt not in codecs:
        raise AudioError(f"unsupported output format: {fmt}")
    cmd = [
        _tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(wav_path), "-vn",
        "-c:a", codecs[fmt], str(dst),
    ]
    res = _run(cmd)
    if res.returncode != 0 or not dst.exists():
        raise AudioError(f"ffmpeg could not encode {fmt} (is the encoder installed?)")
    return dst


# --------------------------------------------------------------------------- #
# WAV (RIFF) helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Pcm:
    data: bytes
    rate: int
    channels: int
    bits: int

    @property
    def duration_s(self) -> float:
        frame = self.channels * self.bits // 8
        return len(self.data) / frame / self.rate if frame and self.rate else 0.0


def parse_wav(blob: bytes) -> Pcm:
    """Parse a RIFF/WAVE PCM file by walking its chunks (does not assume a
    44-byte header). A `data` chunk size that is 0 or 0xFFFFFFFF (streamed
    files) means 'everything until the end'."""
    if len(blob) < 12 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise AudioError("not a RIFF/WAVE file")
    pos = 12
    fmt = None
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        size = struct.unpack("<I", blob[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", blob[body:body + 16])
            if tag not in (1, 0xFFFE):
                raise AudioError(f"unsupported WAV encoding tag {tag}")
            fmt = (channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise AudioError("WAV data chunk before fmt chunk")
            end = len(blob) if size in (0, 0xFFFFFFFF) or body + size > len(blob) else body + size
            return Pcm(data=blob[body:end], rate=fmt[1], channels=fmt[0], bits=fmt[2])
        pos = body + size + (size & 1)
    raise AudioError("WAV file has no data chunk")


def make_wav(pcm: bytes, rate: int = TTS_SAMPLE_RATE, channels: int = 1, bits: int = 16) -> bytes:
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, bits)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def wav_duration(blob: bytes) -> float:
    return parse_wav(blob).duration_s


def concat_wavs(wavs: Sequence[bytes], gap_ms: int = 250) -> bytes:
    """Concatenate PCM WAVs (same format) with `gap_ms` of silence between."""
    if not wavs:
        raise AudioError("nothing to concatenate")
    parts = [parse_wav(w) for w in wavs]
    first = parts[0]
    for p in parts[1:]:
        if (p.rate, p.channels, p.bits) != (first.rate, first.channels, first.bits):
            raise AudioError("cannot concatenate audio with different formats")
    frame = first.channels * first.bits // 8
    gap = b"\x00" * (int(first.rate * gap_ms / 1000) * frame)
    pcm = gap.join(p.data for p in parts)
    return make_wav(pcm, first.rate, first.channels, first.bits)


def l16_to_wav(pcm: bytes, rate: int = TTS_SAMPLE_RATE) -> bytes:
    return make_wav(pcm, rate=rate)


# --------------------------------------------------------------------------- #
# Text chunking for long TTS scripts
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^<>\n]{1,40}>")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+|\s+(?=[—–-]\s)")
_NBSP = "\x00"  # placeholder for whitespace inside <tags>


def _protect(text: str) -> str:
    return _TAG_RE.sub(lambda m: m.group(0).replace(" ", _NBSP), text)


def _unprotect(text: str) -> str:
    return text.replace(_NBSP, " ")


def _pack(pieces: List[str], limit: int, joiner: str) -> List[str]:
    out: List[str] = []
    cur = ""
    for piece in pieces:
        if not piece:
            continue
        cand = piece if not cur else cur + joiner + piece
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = piece
    if cur:
        out.append(cur)
    return out


def _split_long(piece: str, limit: int) -> List[str]:
    """Split one over-long piece: sentences, then clauses, then words, then a
    hard cut. Never cuts inside a protected <tag>."""
    if len(piece) <= limit:
        return [piece]
    for splitter in (_SENTENCE_SPLIT, _CLAUSE_SPLIT):
        parts = [p for p in splitter.split(piece) if p]
        if len(parts) > 1:
            packed = _pack(parts, limit, " ")
            out: List[str] = []
            for p in packed:
                out.extend(_split_long(p, limit) if len(p) > limit else [p])
            return out
    words = piece.split()
    if len(words) > 1:
        packed = _pack(words, limit, " ")
        out = []
        for p in packed:
            out.extend(_split_long(p, limit) if len(p) > limit else [p])
        return out
    return [piece[i:i + limit] for i in range(0, len(piece), limit)]  # one giant token


def chunk_text(text: str, max_chars: int = 2500) -> List[str]:
    """Split a script into chunks of at most `max_chars`, preferring paragraph,
    then sentence, then clause, then word boundaries. Inline tags such as
    `<short pause>` are never split. Rejoining the chunks with whitespace gives
    back the original words."""
    if max_chars < 50:
        raise ValueError("max_chars too small")
    text = _protect(text.replace("\r\n", "\n").replace("\r", "\n").strip())
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: List[str] = []
    for para in paragraphs:
        para = re.sub(r"[ \t]*\n[ \t]*", " ", para)
        pieces.extend(_split_long(para, max_chars))
    chunks = _pack(pieces, max_chars, "\n\n")
    return [_unprotect(c) for c in chunks]
