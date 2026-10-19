"""WAV helpers, ffprobe parsing, text chunking (pure logic + tiny ffmpeg runs)."""

import re
import wave
import io

import pytest

from aistudio import AudioError, audio
from conftest import needs_ffmpeg, sine_wav_bytes


def test_make_and_parse_wav_round_trip():
    pcm = b"\x01\x00\x02\x00" * 100
    wav = audio.make_wav(pcm, 24000)
    p = audio.parse_wav(wav)
    assert (p.data, p.rate, p.channels, p.bits) == (pcm, 24000, 1, 16)
    assert wav[:4] == b"RIFF" and len(wav) == 44 + len(pcm)


def test_parse_wav_matches_stdlib_on_a_real_file():
    blob = sine_wav_bytes(0.5)
    p = audio.parse_wav(blob)
    with wave.open(io.BytesIO(blob)) as w:
        assert p.rate == w.getframerate() and p.data == w.readframes(w.getnframes())


def test_parse_wav_skips_extra_chunks_and_streamed_size():
    pcm = b"\x10\x00" * 50
    base = audio.make_wav(pcm, 16000)
    extra = b"LIST" + (4).to_bytes(4, "little") + b"abcd"
    with_list = base[:36] + extra + base[36:]  # LIST chunk before data
    assert audio.parse_wav(with_list).data == pcm
    streamed = base[:40] + b"\xff\xff\xff\xff" + base[44:]  # size = 0xFFFFFFFF
    assert audio.parse_wav(streamed).data == pcm


@pytest.mark.parametrize("junk", [b"", b"RIFF", b"not a wav file at all", b"RIFF\x00\x00\x00\x00WAVE"])
def test_parse_wav_rejects_garbage(junk):
    with pytest.raises(AudioError):
        audio.parse_wav(junk)


def test_concat_wavs_sums_durations_plus_gaps():
    a, b, c = (sine_wav_bytes(s) for s in (1.0, 0.5, 2.0))
    merged = audio.concat_wavs([a, b, c], gap_ms=250)
    assert audio.wav_duration(merged) == pytest.approx(1.0 + 0.5 + 2.0 + 2 * 0.25, abs=1e-3)
    with wave.open(io.BytesIO(merged)) as w:  # valid per the stdlib reader
        assert w.getnframes() == int(3.5 * 24000) + 2 * 6000


def test_concat_wavs_rejects_mixed_formats_and_empty():
    with pytest.raises(AudioError):
        audio.concat_wavs([sine_wav_bytes(0.1, rate=24000), sine_wav_bytes(0.1, rate=16000)])
    with pytest.raises(AudioError):
        audio.concat_wavs([])


def test_l16_to_wav():
    pcm = b"\x00\x01" * 240
    wav = audio.l16_to_wav(pcm, 24000)
    assert audio.parse_wav(wav).data == pcm


FFPROBE_JSON = {
    "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le", "sample_rate": "24000", "channels": 1,
                 "sample_fmt": "s16", "bits_per_sample": 16, "duration": "12.500000"}],
    "format": {"format_name": "wav", "duration": "12.5"},
}


def test_parse_ffprobe_fields():
    info = audio.parse_ffprobe(FFPROBE_JSON, size_bytes=600044)
    assert info.duration_s == 12.5 and info.sample_rate == 24000 and info.channels == 1
    assert info.is_wav_24k_mono_16


def test_parse_ffprobe_rejects_video_only_and_zero_duration():
    with pytest.raises(AudioError):
        audio.parse_ffprobe({"streams": [{"codec_type": "video"}], "format": {}}, 1)
    with pytest.raises(AudioError):
        audio.parse_ffprobe({"streams": [{"codec_type": "audio"}], "format": {"duration": "0"}}, 1)


def test_parse_volumedetect():
    err = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -23.4 dB\n[Parsed_volumedetect_0 @ 0x1] max_volume: -3.1 dB\n"
    stats = audio.parse_volumedetect(err)
    assert (stats.mean_db, stats.max_db) == (-23.4, -3.1)
    assert audio.parse_volumedetect("mean_volume: -inf dB").mean_db == float("-inf")
    assert audio.parse_volumedetect("nothing").mean_db is None


@needs_ffmpeg
def test_probe_and_levels_on_a_synthetic_file(make_wav):
    wav = make_wav("tone.wav", seconds=2.0)
    info = audio.probe(wav)
    assert info.duration_s == pytest.approx(2.0, abs=0.05) and info.is_wav_24k_mono_16
    stats = audio.level_stats(wav)
    assert stats.max_db is not None and -12 < stats.max_db < -6  # 0.3 amplitude ~ -10.5 dB


@needs_ffmpeg
def test_probe_rejects_non_audio(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(b"this is not audio")
    with pytest.raises(AudioError):
        audio.probe(f)


@needs_ffmpeg
def test_to_wav_24k_mono_converts_and_passes_through(make_wav):
    tmp = audio.make_temp_dir()
    try:
        ok = make_wav("ok.wav", seconds=1.0)
        assert audio.to_wav_24k_mono(ok, tmp, "x") == ok  # already conforming
        stereo = make_wav("st.wav", seconds=1.0, rate=44100, channels=2)
        out = audio.to_wav_24k_mono(stereo, tmp, "conv")
        info = audio.probe(out)
        assert out.parent == tmp and info.is_wav_24k_mono_16
    finally:
        audio.remove_temp_dir(tmp)
    assert not tmp.exists()


def test_temp_dir_is_private_and_outside_repo():
    from aistudio import privacy

    tmp = audio.make_temp_dir()
    try:
        assert not privacy.is_inside(tmp, privacy.forbidden_roots())
        assert (tmp.stat().st_mode & 0o077) == 0
    finally:
        audio.remove_temp_dir(tmp)


# ---- text chunker ---------------------------------------------------------- #
LONG = (
    "Первый абзац. Он состоит из нескольких предложений! Правда? Да, правда.\n\n"
    "Second paragraph, with a pause <short pause> inside, and a laugh <laugh> at the end. "
    + "Another sentence goes here. " * 40
    + "\n\n"
    + "слово " * 300
)


@pytest.mark.parametrize("limit", [60, 120, 500, 2500])
def test_chunks_respect_the_limit_and_keep_all_words(limit):
    chunks = audio.chunk_text(LONG, limit)
    assert chunks and all(0 < len(c) <= limit for c in chunks)
    norm = lambda t: re.sub(r"\s+", " ", t).strip()  # noqa: E731
    assert norm(" ".join(chunks)) == norm(LONG)


def test_chunks_never_split_inline_tags():
    text = ("word " * 30 + "<short pause> " + "more " * 30) * 5
    for limit in (60, 100, 150):
        for chunk in audio.chunk_text(text, limit):
            assert chunk.count("<") == chunk.count(">")
            assert "short pause" not in chunk.replace("<short pause>", "")


def test_short_text_is_one_chunk_and_empty_is_none():
    assert audio.chunk_text("Привет, мир.") == ["Привет, мир."]
    assert audio.chunk_text("   \n\n ") == []


def test_one_giant_token_is_hard_cut():
    chunks = audio.chunk_text("x" * 500, 100)
    assert [len(c) for c in chunks] == [100] * 5


def test_chunk_limit_guard():
    with pytest.raises(ValueError):
        audio.chunk_text("hello", 10)
