"""Transcript parsing and the SRT / VTT / JSON / TXT writers."""

import json

import pytest

from aistudio import ApiError, UsageError, stt
from conftest import load_fixture


@pytest.fixture(params=["generate-content", "interactions"])
def diarized(request):
    name = "stt_generate_content_diarized.json" if request.param == "generate-content" else "stt_interactions_diarized.json"
    return stt.parse_response(request.param, load_fixture(name))


def test_both_response_shapes_parse_to_the_same_transcript(diarized):
    assert diarized.speakers == ["spk_1", "spk_2"]
    assert [s.text for s in diarized.segments] == ["Привет, как дела?", "Хорошо, спасибо."]
    assert [w.text for w in diarized.words] == ["Привет,", "как", "дела?", "Хорошо,", "спасибо."]
    first, last = diarized.words[0], diarized.words[-1]
    assert (first.start, first.end, first.speaker) == (0.1, 0.5, "spk_1")
    assert (last.start, last.end, last.speaker) == (3.2, 3.9, "spk_2")
    assert diarized.segments[1].start == pytest.approx(2.5) and diarized.segments[1].end == pytest.approx(3.9)


def test_word_offsets_are_monotonic(diarized):
    times = [(w.start, w.end) for w in diarized.words]
    assert all(a <= b for a, b in times)
    assert all(times[i][1] <= times[i + 1][0] for i in range(len(times) - 1))


def test_plain_responses():
    gc = stt.parse_generate_content(load_fixture("stt_generate_content_plain.json"))
    assert gc.text == "Привет, как дела? Хорошо, спасибо." and gc.speakers == [] and gc.words == []
    ix = stt.parse_interactions(load_fixture("stt_interactions_plain.json"))
    assert ix.text == "Привет, как дела?"


def test_empty_responses_are_api_errors():
    with pytest.raises(ApiError):
        stt.parse_generate_content({"candidates": []})
    with pytest.raises(ApiError):
        stt.parse_generate_content({"candidates": [{"content": {"parts": []}}]})
    with pytest.raises(ApiError):
        stt.parse_interactions({"steps": []})


def test_srt_and_vtt_cues(diarized):
    srt = stt.render("srt", diarized, {})
    assert srt.startswith("1\n00:00:00,100 --> 00:00:01,400\n[spk_1] Привет, как дела?\n")
    assert "2\n00:00:02,500 --> 00:00:03,900\n[spk_2] Хорошо, спасибо.\n" in srt
    vtt = stt.render("vtt", diarized, {})
    assert vtt.startswith("WEBVTT\n") and "00:00:02.500 --> 00:00:03.900" in vtt


def test_srt_requires_word_timestamps():
    tr = stt.parse_generate_content(load_fixture("stt_generate_content_plain.json"))
    with pytest.raises(UsageError):
        stt.render("srt", tr, {})


def test_txt_with_and_without_speakers(diarized):
    assert stt.render("txt", diarized, {}) == "spk_1: Привет, как дела?\nspk_2: Хорошо, спасибо.\n"
    plain = stt.parse_interactions(load_fixture("stt_interactions_plain.json"))
    assert stt.render("txt", plain, {}) == "Привет, как дела?\n"


def test_json_output_has_structure_and_no_source_filename(diarized):
    meta = {"model": "gemini-3.5-transcribe", "api": "generate-content", "duration_s": 4.0}
    data = json.loads(stt.render("json", diarized, meta))
    assert data["model"] == "gemini-3.5-transcribe" and data["speakers"] == ["spk_1", "spk_2"]
    assert len(data["segments"]) == 2 and len(data["words"]) == 5
    assert data["words"][0] == {"text": "Привет,", "start": 0.1, "end": 0.5, "speaker": "spk_1"}
    assert set(data) == {"model", "api", "duration_s", "text", "speakers", "segments", "words"}


def test_cue_breaking_rules():
    words = [stt.Word("a", 0.0, 0.4), stt.Word("b", 0.5, 0.9), stt.Word("c", 3.0, 3.4),  # pause > 1 s
             stt.Word("d", 3.5, 3.9, speaker="x")]  # speaker change
    cues = stt.make_cues(words)
    assert [c.text for c in cues] == ["a b", "c", "[x] d"]
    long_words = [stt.Word("w" * 10, i * 0.5, i * 0.5 + 0.4) for i in range(30)]
    assert all(len(c.text) <= 84 + 10 for c in stt.make_cues(long_words))
    assert all(c.end - c.start <= 6.5 for c in stt.make_cues(long_words))


def test_offsets_parse():
    from aistudio.client import parse_offset

    assert parse_offset("3.5s") == 3.5 and parse_offset("0.100s") == 0.1 and parse_offset(2) == 2.0
    assert parse_offset(None) is None and parse_offset("") is None and parse_offset("abc") is None


def test_duration_limits():
    from aistudio.audio import AudioInfo

    def info(sec):
        return AudioInfo(sec, 16000, 1, "pcm_s16le", "s16", 16, 1000, "wav")

    stt.check_duration(info(3500), stt.SttOptions())
    with pytest.raises(Exception) as exc:
        stt.check_duration(info(3700), stt.SttOptions())
    assert exc.value.exit_code == 4
    stt.check_duration(info(1700), stt.SttOptions(diarize=True))
    with pytest.raises(Exception):
        stt.check_duration(info(1900), stt.SttOptions(words=True))
