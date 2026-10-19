"""
LIVE tests against the real Gemini API. Opt-in only:

    GAIA_LIVE=1 GEMINI_API_KEY=... python -m pytest tests/aistudio/test_live.py -v

(or a key in ~/.config/gaia/aistudio.env). They use SYNTHETIC audio only: a short
TTS clip from a prebuilt voice is generated and then transcribed. They never
create a replicated voice (that needs the account holder's own recordings and
is a manual step, see README) and never touch private audio. Cost: a few cents.
Skipped silently when GAIA_LIVE is not "1" or no key is available.
"""

import io
import json
import os
import re
import wave

import pytest

import aistudio_speech as cli
from aistudio import config
from conftest import INVALID_KEY

LIVE = os.environ.get("GAIA_LIVE") == "1"


def _real_key():
    """Read the key BEFORE the autouse fixture empties the environment."""
    if not LIVE:
        return None
    try:
        return config.load_settings().api_key
    except Exception:  # noqa: BLE001
        return None


REAL_KEY = _real_key()
REAL_PAID = os.environ.get(config.ENV_PAID_FLAG)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="live tests are opt-in: set GAIA_LIVE=1"),
    pytest.mark.skipif(LIVE and not REAL_KEY, reason="GAIA_LIVE=1 but no API key is configured"),
]

SENTENCE = "Добрый день. Сегодня хорошая погода, и мы проверяем синтез речи."
WER_LIMIT = 0.5  # lenient until the first real run; tighten afterwards


@pytest.fixture(params=["generate-content", "interactions"])
def api(request):
    return request.param


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", REAL_KEY)
    config.register_secret(REAL_KEY)
    return cli


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def words_of(text):
    return re.findall(r"\w+", text.lower())


def wer(ref, hyp):
    r, h = words_of(ref), words_of(hyp)
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r)][len(h)] / max(len(r), 1)


def wav_info(path):
    with wave.open(io.BytesIO(path.read_bytes())) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes() / w.getframerate()


def test_voices_list_returns_russian_voices(live, capsys):
    code, out, err = run(capsys, "voices", "list", "--lang", "ru-RU", "--type", "prebuilt")
    assert code == 0, err
    assert "ru-RU" in out and "prebuilt" in out


def test_wrong_key_exits_3(capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", INVALID_KEY)
    code, out, err = run(capsys, "voices", "list", "--page-size", "1")
    assert code == 3, err


def test_bad_model_name_gives_a_clear_message(live, capsys, tmp_path, api):
    code, out, err = run(capsys, "--api", api, "tts", "--text", "Hello.", "--voice", "Kore",
                         "--model", "gemini-0-does-not-exist", "--out-dir", str(tmp_path / "o"))
    assert code == 5 and "error:" in err


def test_tts_then_stt_round_trip(live, capsys, tmp_path, api):
    out_dir = tmp_path / "audio"
    code, out, err = run(capsys, "--api", api, "tts", "--text", SENTENCE, "--voice", "Kore",
                         "--out-dir", str(out_dir), "--name", "live-ru")
    assert code == 0, err
    wav = out_dir / "live-ru.wav"
    rate, channels, width, seconds = wav_info(wav)
    assert (rate, channels, width) == (24000, 1, 2) and 1.5 < seconds < 20

    txt_dir = tmp_path / "txt"
    code, out, err = run(capsys, "--api", api, "stt", "--input", str(wav), "--out-dir", str(txt_dir),
                         "--lang", "ru-RU", "--accept-unpaid-terms", "--name", "rt")
    assert code == 0, err
    hypothesis = (txt_dir / "rt.txt").read_text(encoding="utf-8")
    assert wer(SENTENCE, hypothesis) <= WER_LIMIT, "round-trip WER above the limit; listen to the clip"


def test_two_speaker_clip_diarizes_with_monotonic_words(live, capsys, tmp_path, api):
    script = tmp_path / "dialogue.txt"
    script.write_text(
        "Anna: Здравствуйте, это первая реплика нашего небольшого разговора, она достаточно длинная.\n"
        "Boris: Добрый день, а это вторая реплика, и ее говорит совсем другой голос.\n"
        "Anna: Отлично, тогда мы проверим, сколько говорящих найдет распознавание.\n",
        encoding="utf-8",
    )
    audio_dir = tmp_path / "audio"
    code, out, err = run(capsys, "--api", api, "tts", "--text-file", str(script), "--speaker", "Anna=Kore",
                         "--speaker", "Boris=Charon", "--out-dir", str(audio_dir), "--name", "dialogue", "--gap-ms", "600")
    assert code == 0, err
    _, _, _, seconds = wav_info(audio_dir / "dialogue.wav")

    txt_dir = tmp_path / "txt"
    code, out, err = run(capsys, "--api", api, "stt", "--input", str(audio_dir / "dialogue.wav"),
                         "--out-dir", str(txt_dir), "--lang", "ru-RU", "--diarize", "--words",
                         "--format", "json", "--accept-unpaid-terms", "--name", "d")
    assert code == 0, err
    data = json.loads((txt_dir / "d.json").read_text(encoding="utf-8"))
    assert len(data["speakers"]) >= 2
    starts = [w["start"] for w in data["words"] if w["start"] is not None]
    assert starts == sorted(starts) and starts[-1] <= seconds + 0.5
