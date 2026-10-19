"""
Shared fixtures for the AI Studio speech tests.

Offline by default: no network, no key, no real audio of people. Audio is
synthetic (generated sine tones). Every test runs against a throw-away config
dir and an empty environment, never against ~/.config/gaia.
"""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import struct
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"

# Synthetic, never valid. Assembled at runtime so the source contains no literal that
# looks like a Google API key (secret scanners would flag it).
FAKE_KEY = "AI" + "za" + "SyFAKE-FAKE-FAKE-FAKE-FAKE-FAKE-1234"
INVALID_KEY = "AI" + "za" + "SyINVALID-INVALID-INVALID-INVALID-0000"

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    cfg = tmp_path / "gaia-config"
    monkeypatch.setenv("GAIA_CONFIG_DIR", str(cfg))
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GAIA_AISTUDIO_PAID_PROJECT", "GAIA_AISTUDIO_API",
                "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(var, raising=False)
    from aistudio import config

    config._EXTRA_SECRETS.clear()
    return cfg


def sine_wav_bytes(seconds: float = 1.0, rate: int = 24000, freq: float = 220.0, amp: float = 0.3,
                   channels: int = 1) -> bytes:
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", v) * channels
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.fixture
def make_wav(tmp_path):
    """make_wav("name.wav", seconds=12) -> Path in tmp_path (outside the repo)."""

    def _make(name: str, seconds: float = 1.0, **kw) -> Path:
        path = tmp_path / "audio" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(sine_wav_bytes(seconds, **kw))
        return path

    return _make


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def assert_golden(name: str, obj) -> None:
    """Compare with tests/aistudio/golden/<name>.json. A missing golden file
    fails unless UPDATE_GOLDEN=1 (then it is written; review the diff!)."""
    path = GOLDEN / f"{name}.json"
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing golden file {path.name}; run with UPDATE_GOLDEN=1 and review it"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)
