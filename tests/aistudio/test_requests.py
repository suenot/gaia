"""Request builders produce the documented JSON (golden files, synthetic content).

Goldens live in tests/aistudio/golden/. They encode the shapes from:
  https://ai.google.dev/gemini-api/docs/speech-generation
  https://ai.google.dev/gemini-api/docs/transcribe
  https://ai.google.dev/gemini-api/docs/voice-replication
  https://ai.google.dev/api/generate-content   (REST reference for generateContent)
"""

import pytest

from aistudio import UsageError, stt, tts, voice_clone, voices
from aistudio.client import API_GENERATE_CONTENT, API_INTERACTIONS, describe_body
from conftest import assert_golden

GC, IX = API_GENERATE_CONTENT, API_INTERACTIONS
FLASH, LITE = voices.MODEL_FLASH, voices.MODEL_FLASH_LITE
TEXT = "Hello there. <short pause> This is a synthetic sentence."


# ---- TTS ------------------------------------------------------------------ #
def test_tts_single_generate_content():
    assert_golden("tts_single_generate_content", tts.build_speech_request(FLASH, TEXT, "Kore", api=GC).golden())


def test_tts_style_and_lang_generate_content():
    req = tts.build_speech_request(LITE, TEXT, "voice_abc123", style="calm, warm", lang="ru-RU", api=GC)
    assert_golden("tts_style_lang_generate_content", req.golden())


def test_tts_voicekey_generate_content():
    req = tts.build_speech_request(LITE, TEXT, "voicekey_SYNTHETIC0123456789", api=GC)
    assert_golden("tts_voicekey_generate_content", req.golden())


def test_tts_single_interactions_is_stateless():
    req = tts.build_speech_request(FLASH, TEXT, "Kore", style="cheerful and friendly", api=IX)
    assert req.body["store"] is False
    assert_golden("tts_single_interactions", req.golden())


def test_tts_no_style_means_no_speech_metadata():
    body = tts.build_speech_request(FLASH, TEXT, "Kore", api=GC).body
    assert "speechMetadata" not in body["contents"][0]["parts"][0]
    body = tts.build_speech_request(FLASH, TEXT, "Kore", api=IX).body
    assert "annotations" not in body["input"][0]["content"][0]


def test_tts_rejects_empty_text_and_interactions_lang():
    with pytest.raises(UsageError):
        tts.build_speech_request(FLASH, "  ", "Kore")
    with pytest.raises(UsageError):
        tts.build_speech_request(FLASH, TEXT, "Kore", lang="ru-RU", api=IX)


def test_tags_stay_in_the_text_and_directions_stay_out_of_it():
    req = tts.build_speech_request(FLASH, "Wait... <short pause> look! <laugh>", "Puck", style="whispered urgently")
    part = req.body["contents"][0]["parts"][0]
    assert part["text"] == "Wait... <short pause> look! <laugh>"
    assert part["speechMetadata"] == {"style": "whispered urgently"}


def test_dialogue_script_parsing():
    script = "A: Привет!\nB: Привет. Как дела?\nпродолжение реплики B\nA: Хорошо."
    turns = tts.parse_turns(script, ["A", "B"])
    assert [(t.speaker, t.text) for t in turns] == [
        ("A", "Привет!"), ("B", "Привет. Как дела? продолжение реплики B"), ("A", "Хорошо."),
    ]
    with pytest.raises(UsageError):
        tts.parse_turns("no label here", ["A"])
    assert tts.parse_speaker_args(["A=Kore", "B=me"]) == {"A": "Kore", "B": "me"}
    for bad in (["A"], ["=x"], ["A=Kore", "A=Puck"]):
        with pytest.raises(UsageError):
            tts.parse_speaker_args(bad)


def test_cost_estimate_uses_documented_prices():
    est = tts.estimate_cost(1400, FLASH)  # 100 s at 14 chars/s
    assert est.seconds == pytest.approx(100)
    assert est.usd_now == pytest.approx(0.0225) and est.usd_2027 == pytest.approx(0.045)
    lite = tts.estimate_cost(1400, LITE)
    assert lite.usd_now == pytest.approx(0.015) and lite.usd_2027 == pytest.approx(0.03)
    assert tts.estimate_cost(10, "some-other-model") is None


# ---- STT ------------------------------------------------------------------ #
FILE = stt.AudioPart("audio/wav", uri="https://generativelanguage.googleapis.com/v1beta/files/SYNTHETIC")
INLINE = stt.AudioPart("audio/wav", data_b64="U1lOVEhFVElD")  # base64 of "SYNTHETIC"


def test_stt_plain_both_apis():
    assert_golden("stt_plain_generate_content", stt.build_transcribe_request(FILE, stt.SttOptions(), api=GC).golden())
    assert_golden("stt_plain_interactions", stt.build_transcribe_request(FILE, stt.SttOptions(), api=IX).golden())


def test_stt_language_hint_inline_audio():
    opts = stt.SttOptions(language_codes=["ru-RU"])
    assert_golden("stt_lang_inline_generate_content", stt.build_transcribe_request(INLINE, opts, api=GC).golden())
    assert_golden("stt_lang_inline_interactions", stt.build_transcribe_request(INLINE, opts, api=IX).golden())


def test_stt_diarization_and_words_both_apis():
    opts = stt.SttOptions(language_codes=["ru-RU"], diarize=True, words=True)
    assert_golden("stt_diarize_words_generate_content", stt.build_transcribe_request(FILE, opts, api=GC).golden())
    assert_golden("stt_diarize_words_interactions", stt.build_transcribe_request(FILE, opts, api=IX).golden())


def test_stt_diarization_only_and_words_only_interactions_modes():
    d = stt.build_transcribe_request(FILE, stt.SttOptions(diarize=True), api=IX).body
    assert d["generation_config"]["transcription_config"]["mode"] == {"type": "verbatim", "diarization_mode": "speaker"}
    w = stt.build_transcribe_request(FILE, stt.SttOptions(words=True), api=IX).body
    assert w["generation_config"]["transcription_config"]["mode"] == {
        "type": "verbatim", "timestamp_granularities": ["word"]}


def test_stt_smart_and_vocabulary():
    smart = stt.SttOptions(smart=True)
    assert_golden("stt_smart_generate_content", stt.build_transcribe_request(FILE, smart, api=GC).golden())
    assert stt.build_transcribe_request(FILE, smart, api=IX).body["generation_config"]["transcription_config"]["mode"] == "smart"
    vocab = stt.SttOptions(vocabulary=["Gaia", "Camoufox"])
    assert_golden("stt_vocab_generate_content", stt.build_transcribe_request(FILE, vocab, api=GC).golden())


@pytest.mark.parametrize(
    "opts",
    [
        stt.SttOptions(vocabulary=["x"], diarize=True),
        stt.SttOptions(vocabulary=["x"], words=True),
        stt.SttOptions(smart=True, diarize=True),
        stt.SttOptions(smart=True, words=True),
        stt.SttOptions(vocabulary=[str(i) for i in range(1001)]),
    ],
)
def test_stt_incompatible_combinations_are_rejected_locally(opts):
    for api in (GC, IX):
        with pytest.raises(UsageError):
            stt.build_transcribe_request(FILE, opts, api=api)


def test_stt_requests_never_carry_a_file_name():
    for api in (GC, IX):
        text = str(stt.build_transcribe_request(FILE, stt.SttOptions(), api=api).body)
        assert ".wav" not in text and "call" not in text.lower()


# ---- Voices --------------------------------------------------------------- #
def test_create_voice_request_stateless_and_stored():
    stateless = voice_clone.build_create_request(LITE, False, "U1lOVEhFVElD", "Q09OU0VOVA==", display_name="ignored")
    assert stateless["store"] is False and "display_name" not in stateless["voice"]
    assert_golden("voices_create_stateless", stateless)
    stored = voice_clone.build_create_request(FLASH, True, "U1lOVEhFVElD", "Q09OU0VOVA==", display_name="me")
    assert stored["store"] is True and stored["voice"]["display_name"] == "me"
    assert_golden("voices_create_stored", stored)


def test_create_voice_always_passes_store_explicitly():
    for store in (True, False):
        assert voice_clone.build_create_request(LITE, store, "a", "b")["store"] is store


def test_list_filters_map_to_sdk_arguments():
    f = voices.list_filters(lang="ru-RU", type_="prebuilt", gender="female", pitch="low", search="warm", page_size=5)
    assert_golden("voices_list_filters", f)
    with pytest.raises(UsageError):
        voices.list_filters(type_="nonsense")


def test_describe_body_elides_text_audio_and_voicekeys():
    req = tts.build_speech_request(LITE, "Секретный текст", "voicekey_SYNTHETIC0123456789", api=GC)
    printed = str(req.printable())
    assert "Секретный" not in printed and "SYNTHETIC0123456789" not in printed
    create = voice_clone.build_create_request(LITE, False, "QUJD" * 50, "REVG" * 50)
    assert "QUJDQUJD" not in str(describe_body(create))
