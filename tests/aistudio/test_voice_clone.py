"""Voice replication guard rails, preflight rules, consent log. All offline:
the API is a fake that records what it was given; audio is synthetic tones."""

import hashlib
import json
import stat

import pytest

from aistudio import ApiError, AudioError, UsageError, audio, privacy, voice_clone, voices
from aistudio.audio import AudioInfo, LevelStats
from aistudio.voice_clone import ClipReport
from conftest import needs_ffmpeg

LITE = voices.MODEL_FLASH_LITE


def clip(duration, mean=-25.0, peak=-6.0, channels=1):
    return ClipReport(AudioInfo(duration, 24000, channels, "pcm_s16le", "s16", 16, 1000, "wav"), LevelStats(mean, peak))


# ---- pure rule checks -------------------------------------------------------- #
def test_good_clips_pass_without_warnings():
    pre = voice_clone.check_clips(clip(20.0), clip(6.0))
    assert pre.warnings == []


@pytest.mark.parametrize("seconds", [5.0, 9.0, 31.0, 60.0])
def test_reference_outside_10_to_30_seconds_is_refused(seconds):
    with pytest.raises(AudioError) as exc:
        voice_clone.check_clips(clip(seconds), clip(6.0))
    assert exc.value.exit_code == 4 and "10-30" in str(exc.value)


def test_reference_slightly_outside_window_only_warns():
    pre = voice_clone.check_clips(clip(9.7), clip(6.0))
    assert any("slightly outside" in w for w in pre.warnings)


@pytest.mark.parametrize("seconds", [0.5, 45.0])
def test_consent_clip_length_limits(seconds):
    with pytest.raises(AudioError):
        voice_clone.check_clips(clip(20.0), clip(seconds))


def test_warnings_for_clipping_quiet_stereo_long_consent_and_level_mismatch():
    pre = voice_clone.check_clips(clip(20.0, mean=-50.0, peak=0.0, channels=2), clip(20.0, mean=-20.0))
    text = " | ".join(pre.warnings)
    for needle in ("clipping", "very quiet", "downmixed", "longer than expected", "differ a lot"):
        assert needle in text


# ---- consent log ---------------------------------------------------------------- #
def test_consent_record_has_only_hashes_ids_and_metadata():
    rec = voice_clone.consent_record(
        alias="me", lang="ru-RU", model=LITE, store=False, reference_sha256="a" * 64, consent_sha256="b" * 64,
        voice_id=None, voice_key="voicekey_SYNTHETIC0123456", timestamp="2026-10-19T12:00:00Z")
    assert rec["statement_sha256"] == hashlib.sha256(voices.consent_statement("ru-RU").encode()).hexdigest()
    assert rec["voice_key_sha256"] == hashlib.sha256(b"voicekey_SYNTHETIC0123456").hexdigest()
    assert "SYNTHETIC0123456" not in json.dumps(rec)
    assert set(rec) == {"ts", "alias", "language", "statement_sha256", "consent_audio_sha256",
                        "reference_audio_sha256", "voice_id", "voice_key_sha256", "model", "store"}


def test_consent_log_is_append_only_and_private(tmp_path):
    path = tmp_path / "cfg" / "voice-consent.jsonl"
    voice_clone.append_consent_log(path, {"n": 1})
    voice_clone.append_consent_log(path, {"n": 2})
    assert [json.loads(l)["n"] for l in path.read_text().splitlines()] == [1, 2]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_parse_create_response():
    assert voice_clone.parse_create_response({"id": "voice_a", "expire_time": "x"}, True) == ("voice_a", None, "x")
    assert voice_clone.parse_create_response({"key": "voicekey_k"}, False) == (None, "voicekey_k", None)
    with pytest.raises(ApiError):
        voice_clone.parse_create_response({"key": "voicekey_k"}, True)
    with pytest.raises(ApiError):
        voice_clone.parse_create_response({"id": "voice_a"}, False)


def test_audition_sentence_language():
    assert "голоса" in voice_clone.audition_sentence("ru-RU")
    assert voice_clone.audition_sentence("de-DE").startswith("Hello")


# ---- orchestration with a fake API ---------------------------------------------- #
class FakeApi:
    def __init__(self, response=None):
        self.requests = []
        self.response = response or {"key": "voicekey_SYNTHETIC0123456789", "type": "replicated",
                                     "expire_time": "2026-10-26T00:00:00Z"}

    def create_voice(self, request):
        self.requests.append(request)
        return self.response


@pytest.fixture
def clips(make_wav):
    return make_wav("ref.wav", seconds=12.0, freq=200), make_wav("consent.wav", seconds=5.0, freq=200)


def run(registry, tmp_path, ref, consent, api=None, **kw):
    args = dict(reference=str(ref), consent=str(consent), lang="ru-RU", alias="me", store=False,
                i_am_the_speaker=True)
    args.update(kw)
    messages = []
    result = voice_clone.replicate(api, registry, tmp_path / "log.jsonl", progress=messages.append, **args)
    return result, messages


def test_requires_the_speaker_flag(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    with pytest.raises(UsageError) as exc:
        run(reg, tmp_path, *clips, api=FakeApi(), i_am_the_speaker=False)
    assert "--i-am-the-speaker" in str(exc.value)


@needs_ffmpeg
def test_dry_run_prints_statement_checks_clips_and_sends_nothing(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    result, messages = run(reg, tmp_path, *clips, api=None, dry_run=True)
    assert result.dry_run and result.entry is None
    assert voices.consent_statement("ru-RU") in "\n".join(messages)
    body = result.request
    assert body["store"] is False and body["voice"]["type"] == "replicated"
    assert body["voice"]["replicated"]["source_audio"]["data"].startswith("<inline audio:")
    assert not reg.path.exists() and not (tmp_path / "log.jsonl").exists()


@needs_ffmpeg
def test_full_flow_stateless(tmp_path, clips):
    reg = voices.Registry(tmp_path / "cfg" / "voices.json")
    api = FakeApi()
    result, messages = run(reg, tmp_path, *clips, api=api, confirm=lambda phrase: True)
    sent = api.requests[0]
    assert sent["store"] is False and sent["voice"]["model"] == LITE and "display_name" not in sent["voice"]
    # the clips were converted/passed through as 24 kHz mono WAV, base64
    import base64

    for key in ("source_audio", "consent_audio"):
        blob = base64.b64decode(sent["voice"]["replicated"][key]["data"])
        assert audio.parse_wav(blob).rate == 24000
    entry = reg.get("me")
    assert entry and not entry.store and entry.voice_key == "voicekey_SYNTHETIC0123456789"
    assert entry.expires == "2026-10-26T00:00:00Z"
    log = [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert len(log) == 1 and log[0]["alias"] == "me" and log[0]["language"] == "ru-RU" and log[0]["store"] is False
    assert log[0]["consent_audio_sha256"] == privacy.sha256_file(clips[1])
    assert log[0]["reference_audio_sha256"] == privacy.sha256_file(clips[0])
    assert log[0]["voice_id"] is None and "SYNTHETIC0123456789" not in json.dumps(log)
    assert "voicekey_" not in "\n".join(messages) and "SYNTHETIC0123456789" not in "\n".join(messages)


@needs_ffmpeg
def test_full_flow_stored(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    api = FakeApi({"id": "voice_SYNTH1", "type": "replicated", "expire_time": "2027-10-19T00:00:00Z"})
    run(reg, tmp_path, *clips, api=api, store=True, alias="narrator")
    sent = api.requests[0]
    assert sent["store"] is True and sent["voice"]["display_name"] == "narrator"
    entry = reg.get("narrator")
    assert entry.store and entry.voice_id == "voice_SYNTH1" and entry.voice_key is None


@needs_ffmpeg
def test_temp_files_are_removed(tmp_path, clips, monkeypatch):
    made = []
    real = audio.make_temp_dir

    def spy():
        d = real()
        made.append(d)
        return d

    monkeypatch.setattr(audio, "make_temp_dir", spy)
    reg = voices.Registry(tmp_path / "voices.json")
    run(reg, tmp_path, *clips, api=FakeApi())
    assert made and all(not d.exists() for d in made)


@needs_ffmpeg
def test_declined_confirmation_sends_nothing(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    api = FakeApi()
    with pytest.raises(UsageError):
        run(reg, tmp_path, *clips, api=api, confirm=lambda phrase: False)
    assert api.requests == [] and not reg.path.exists()


@needs_ffmpeg
def test_existing_alias_is_refused_before_any_upload(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    api = FakeApi()
    run(reg, tmp_path, *clips, api=api)
    with pytest.raises(UsageError):
        run(reg, tmp_path, *clips, api=api)
    assert len(api.requests) == 1


def test_clip_inside_repo_is_refused(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    with pytest.raises(AudioError):
        run(reg, tmp_path, privacy.PACKAGE_ROOT / "assets" / "gaia_comic.png", clips[1], api=FakeApi())


def test_same_file_twice_is_refused(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    with pytest.raises(UsageError):
        run(reg, tmp_path, clips[0], clips[0], api=FakeApi())


@needs_ffmpeg
def test_short_reference_is_refused_before_upload(tmp_path, make_wav):
    reg = voices.Registry(tmp_path / "voices.json")
    api = FakeApi()
    with pytest.raises(AudioError):
        run(reg, tmp_path, make_wav("short.wav", seconds=4.0), make_wav("c.wav", seconds=5.0), api=api)
    assert api.requests == []


def test_unsupported_language_is_refused(tmp_path, clips):
    reg = voices.Registry(tmp_path / "voices.json")
    with pytest.raises(UsageError):
        run(reg, tmp_path, *clips, api=FakeApi(), lang="xx-ZZ")
