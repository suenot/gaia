"""The CLI end to end, in-process: exit codes, --dry-run paths, privacy rules,
and that nothing sensitive reaches stdout/stderr or the output directory."""

import json
import stat

import pytest

import aistudio_speech as cli
from aistudio import config, privacy, voices
from conftest import FAKE_KEY, b64, load_fixture, needs_ffmpeg, sine_wav_bytes

REPO = privacy.PACKAGE_ROOT
SECRET_TEXT = "Конфиденциальный текст сценария номер 7"


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)


class FakeGen:
    """Stands in for GeminiClient: canned answers, records calls."""

    def __init__(self, api="generate-content", response=None):
        self.api = api
        self.response = response
        self.calls = []
        self.uploads = []
        self.deleted = []

    def generate(self, model, body, what):
        self.calls.append((model, body, what))
        return self.response() if callable(self.response) else self.response

    def upload_file(self, path, mime, display_name):
        from aistudio.client import UploadedFile

        self.uploads.append((path, mime, display_name))
        return UploadedFile("files/SYNTH", "https://x/files/SYNTH", mime)

    def delete_file(self, name):
        self.deleted.append(name)
        return True


def patch_client(monkeypatch, fake, paid=True):
    settings = config.Settings(api_key=FAKE_KEY, key_source="env", config_dir=config.config_dir(), paid_project=paid)
    monkeypatch.setattr(cli, "make_client", lambda args: (fake, settings))


def tts_response(seconds=0.3):
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/wav", "data": b64(sine_wav_bytes(seconds))}}]}}]}


# ---- consent-text -------------------------------------------------------------- #
def test_consent_text_ru(capsys):
    code, out, _ = run(capsys, "consent-text", "--lang", "ru-RU")
    assert code == 0 and voices.consent_statement("ru-RU") in out


def test_consent_text_list_and_json_and_errors(capsys):
    code, out, _ = run(capsys, "consent-text", "--list")
    assert code == 0 and len(out.strip().splitlines()) == 30 and "ru-RU\tRussian" in out
    code, out, _ = run(capsys, "consent-text", "--lang", "ru", "--json")
    assert json.loads(out) == {"language": "ru-RU", "statement": voices.consent_statement("ru-RU")}
    code, _, err = run(capsys, "consent-text", "--lang", "klingon")
    assert code == 2 and "error:" in err


def test_argparse_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["tts"])
    assert exc.value.code == 2


def test_global_options_work_after_the_subcommand(capsys, monkeypatch):
    code, out, _ = run(capsys, "check", "--dry-run", "--api", "interactions")
    assert code == 0 and "api surface: interactions" in out


def test_bad_api_in_env_is_a_usage_error(capsys, monkeypatch):
    monkeypatch.setenv("GAIA_AISTUDIO_API", "bogus")
    code, _, err = run(capsys, "check", "--dry-run")
    assert code == 2 and "GAIA_AISTUDIO_API" in err


# ---- check --------------------------------------------------------------------- #
def test_check_dry_run_without_key_and_never_prints_the_key(capsys, monkeypatch):
    code, out, err = run(capsys, "check", "--dry-run")
    assert code == 0 and "no key" in out and "no network call" in out
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    code, out, err = run(capsys, "check", "--dry-run")
    assert code == 0 and "set in the environment" in out and FAKE_KEY not in out + err


def test_check_with_key_reports_models_without_leaking(capsys, key_env, monkeypatch):
    class Fake:
        api = "generate-content"

        def list_model_names(self):
            return ["models/gemini-3.8-flash-tts", "models/gemini-3.5-transcribe"]

        def list_voices(self, **kw):
            return {"voices": [{"id": "Kore"}]}

    patch_client(monkeypatch, Fake(), paid=False)
    code, out, err = run(capsys, "check")
    assert code == 0 and "gemini-3.8-flash-tts: listed" in out and "gemini-3.8-flash-lite-tts: not listed" in out
    assert "cannot confirm a billing-linked project" in err and FAKE_KEY not in out + err


def test_commands_without_a_key_exit_3(capsys):
    code, _, err = run(capsys, "voices", "list")
    assert code == 3 and "no API key" in err
    code, _, err = run(capsys, "check")
    assert code == 3


# ---- tts ----------------------------------------------------------------------- #
def test_tts_dry_run_needs_no_key_and_hides_the_text(capsys, tmp_path):
    out_dir = tmp_path / "out"
    code, out, err = run(capsys, "tts", "--text", SECRET_TEXT, "--voice", "Kore", "--out-dir", str(out_dir),
                         "--style", "calm", "--dry-run")
    assert code == 0 and SECRET_TEXT not in out + err and "Конфиденциальный" not in out + err
    assert '"voice": "Kore"' in out and "<text:" in out and "estimate" in out
    assert not out_dir.exists()  # dry run creates nothing


def test_tts_dry_run_masks_voicekeys_and_aliases(capsys, tmp_path):
    code, out, err = run(capsys, "tts", "--text", "hi", "--voice", "voicekey_SYNTHETICSECRET99", "--out-dir", str(tmp_path / "o"), "--dry-run")
    assert code == 0 and "SYNTHETICSECRET99" not in out + err and "voicekey_<masked>" in out
    from aistudio.voices import Registry, VoiceEntry

    Registry(config.registry_path()).add(VoiceEntry("me", voices.MODEL_FLASH_LITE, "ru-RU", False, "2026-10-19T00:00:00Z",
                                                    "2099-01-01T00:00:00Z", voice_key="voicekey_ALIASSECRET1234"))
    code, out, err = run(capsys, "tts", "--text", "hi", "--voice", "me", "--out-dir", str(tmp_path / "o"), "--dry-run")
    assert code == 0 and "ALIASSECRET1234" not in out + err and "alias me" in out


def test_tts_out_dir_inside_repo_is_refused(capsys):
    for sub in ("output", "debug", "."):
        code, _, err = run(capsys, "tts", "--text", "hi", "--voice", "Kore", "--out-dir", str(REPO / sub), "--dry-run")
        assert code == 2 and "inside the repository" in err


def test_tts_voice_xor_speaker(capsys, tmp_path):
    base = ["tts", "--text", "A: hi", "--out-dir", str(tmp_path / "o"), "--dry-run"]
    assert run(capsys, *base)[0] == 2
    assert run(capsys, *base, "--voice", "Kore", "--speaker", "A=Puck")[0] == 2
    assert run(capsys, *base, "--speaker", "A=Puck")[0] == 0


def test_tts_end_to_end_writes_private_unmodified_wav(capsys, monkeypatch, tmp_path):
    wav = sine_wav_bytes(0.3)
    fake = FakeGen(response={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/wav", "data": b64(wav)}}]}}]})
    patch_client(monkeypatch, fake)
    out_dir = tmp_path / "out"
    code, out, err = run(capsys, "tts", "--text", SECRET_TEXT, "--voice", "Kore", "--out-dir", str(out_dir))
    assert code == 0 and SECRET_TEXT not in out + err
    files = list(out_dir.iterdir())
    assert len(files) == 1 and files[0].name.startswith("tts-") and files[0].suffix == ".wav"
    assert files[0].read_bytes() == wav  # single request: bytes untouched (provenance metadata intact)
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600 and stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert "byte-identical" not in err
    # no clobbering without --overwrite
    code, _, err = run(capsys, "tts", "--text", SECRET_TEXT, "--voice", "Kore", "--out-dir", str(out_dir))
    assert code == 2 and "--overwrite" in err
    code, _, _ = run(capsys, "tts", "--text", SECRET_TEXT, "--voice", "Kore", "--out-dir", str(out_dir), "--overwrite")
    assert code == 0
    # the request carried the right voice and kept the text verbatim
    model, body, _ = fake.calls[0]
    assert model == voices.MODEL_FLASH_LITE and body["contents"][0]["parts"][0]["text"] == SECRET_TEXT


def test_tts_stitched_output_warns_about_provenance(capsys, monkeypatch, tmp_path):
    fake = FakeGen(response=lambda: tts_response(0.2))
    patch_client(monkeypatch, fake)
    text = "\n\n".join(["Абзац номер %d. " % i + "слово " * 30 for i in range(6)])
    code, out, err = run(capsys, "tts", "--text", text, "--voice", "Kore", "--out-dir", str(tmp_path / "o"),
                         "--max-chars", "200", "--name", "narration")
    assert code == 0 and len(fake.calls) > 1 and (tmp_path / "o" / "narration.wav").exists()
    assert "C2PA" in err and "stitched locally" in err


def test_tts_dialogue_mode_uses_one_request_per_turn(capsys, monkeypatch, tmp_path):
    fake = FakeGen(response=lambda: tts_response(0.2))
    patch_client(monkeypatch, fake)
    script = tmp_path / "script.txt"
    script.write_text("Anna: Привет!\nBoris: Здравствуй.\nAnna: Как дела?\n", encoding="utf-8")
    code, out, err = run(capsys, "tts", "--text-file", str(script), "--speaker", "Anna=Kore", "--speaker", "Boris=Puck",
                         "--out-dir", str(tmp_path / "o"))
    assert code == 0 and len(fake.calls) == 3
    voices_used = [c[1]["generationConfig"]["speechConfig"]["voiceConfig"]["voice"] for c in fake.calls]
    assert voices_used == ["Kore", "Puck", "Kore"]


@needs_ffmpeg
def test_tts_flac_output(capsys, monkeypatch, tmp_path):
    patch_client(monkeypatch, FakeGen(response=lambda: tts_response(0.3)))
    code, out, err = run(capsys, "tts", "--text", "hello world", "--voice", "Kore", "--out-dir", str(tmp_path / "o"),
                         "--format", "flac", "--name", "x")
    assert code == 0 and (tmp_path / "o" / "x.flac").read_bytes()[:4] == b"fLaC" and "converted to flac" in err


def test_tts_api_error_exit_codes(capsys, monkeypatch, tmp_path):
    from aistudio import ApiError

    def boom():
        raise ApiError("tts: quota or rate limit exhausted (RESOURCE_EXHAUSTED)")

    patch_client(monkeypatch, FakeGen(response=boom))
    code, _, err = run(capsys, "tts", "--text", "hi", "--voice", "Kore", "--out-dir", str(tmp_path / "o"))
    assert code == 5 and "quota" in err


# ---- stt ----------------------------------------------------------------------- #
@needs_ffmpeg
def test_stt_dry_run_shows_request_without_audio(capsys, make_wav, tmp_path):
    wav = make_wav("private-call.wav", seconds=3.0)
    code, out, err = run(capsys, "stt", "--input", str(wav), "--out-dir", str(tmp_path / "o"), "--lang", "ru-RU",
                         "--diarize", "--words", "--dry-run")
    assert code == 0 and "audioTranscriptionConfig" in out and "<inline audio:" in out
    assert "private-call" not in out + err  # the source file name is never echoed
    assert not (tmp_path / "o").exists()


def test_stt_input_rules(capsys, tmp_path):
    out = str(tmp_path / "o")
    assert run(capsys, "stt", "--input", str(REPO / "README.md"), "--out-dir", out, "--dry-run")[0] == 4
    assert run(capsys, "stt", "--input", "relative/call.wav", "--out-dir", out, "--dry-run")[0] == 4
    assert run(capsys, "stt", "--input", str(tmp_path / "missing.wav"), "--out-dir", out, "--dry-run")[0] == 4


@needs_ffmpeg
def test_stt_output_and_option_rules(capsys, make_wav, tmp_path):
    wav = make_wav("a.wav", seconds=2.0)
    ok = ["stt", "--input", str(wav), "--dry-run"]
    assert run(capsys, *ok, "--out-dir", str(REPO / "output"))[0] == 2
    assert run(capsys, *ok, "--out-dir", str(tmp_path / "o"), "--format", "srt")[0] == 2  # needs --words
    assert run(capsys, *ok, "--out-dir", str(tmp_path / "o"), "--term", "x", "--diarize")[0] == 2
    assert run(capsys, *ok, "--out-dir", str(tmp_path / "o"), "--smart", "--words")[0] == 2
    assert run(capsys, *ok, "--out-dir", str(tmp_path / "o"), "--lang", "not a code")[0] == 2
    assert run(capsys, *ok, "--out-dir", str(tmp_path / "o"), "--name", "../evil")[0] == 2


@needs_ffmpeg
def test_stt_requires_paid_acknowledgement(capsys, make_wav, tmp_path, key_env, monkeypatch):
    wav = make_wav("a.wav", seconds=2.0)
    fake = FakeGen(response=load_fixture("stt_generate_content_plain.json"))
    patch_client(monkeypatch, fake, paid=False)
    args = ["stt", "--input", str(wav), "--out-dir", str(tmp_path / "o")]
    code, _, err = run(capsys, *args)
    assert code == 2 and "billing-linked" in err and fake.calls == []
    code, _, err = run(capsys, *args, "--accept-unpaid-terms")
    assert code == 0 and "free tier" in err and len(fake.calls) == 1


@needs_ffmpeg
def test_stt_end_to_end_writes_private_files_and_logs_no_transcript(capsys, make_wav, tmp_path, monkeypatch):
    wav = make_wav("very-private-call.wav", seconds=2.0)
    fake = FakeGen(response=load_fixture("stt_generate_content_diarized.json"))
    patch_client(monkeypatch, fake, paid=True)
    out_dir = tmp_path / "o"
    code, out, err = run(capsys, "stt", "--input", str(wav), "--out-dir", str(out_dir), "--lang", "ru-RU",
                         "--diarize", "--words", "--format", "txt", "--format", "srt", "--format", "json")
    assert code == 0
    combined = out + err
    assert "Привет" not in combined and "спасибо" not in combined  # no transcript text in logs
    assert "very-private-call" not in combined
    names = sorted(p.name for p in out_dir.iterdir())
    assert len(names) == 3 and all(n.startswith("stt-") for n in names) and "very-private-call" not in "".join(names)
    assert all(stat.S_IMODE((out_dir / n).stat().st_mode) == 0o600 for n in names)
    txt = next(out_dir.glob("*.txt")).read_text(encoding="utf-8")
    assert txt == "spk_1: Привет, как дела?\nspk_2: Хорошо, спасибо.\n"
    assert "[spk_2] Хорошо, спасибо." in next(out_dir.glob("*.srt")).read_text(encoding="utf-8")
    data = json.loads(next(out_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert data["diarization"] and data["word_timestamps"] and data["transport"] == "inline"
    assert "very-private-call" not in json.dumps(data)
    # the request had the language hint, diarization and word timestamps
    body = fake.calls[0][1]
    assert body["generationConfig"]["audioTranscriptionConfig"] == {
        "languageCodes": ["ru-RU"], "diarization": True, "wordTimestamp": True}


@needs_ffmpeg
def test_stt_large_audio_goes_through_files_api_and_is_deleted(capsys, make_wav, tmp_path, monkeypatch):
    from aistudio import stt

    wav = make_wav("big.wav", seconds=2.0)
    monkeypatch.setattr(stt, "INLINE_MAX_BYTES", 1000)  # force the upload path
    fake = FakeGen(response=load_fixture("stt_generate_content_plain.json"))
    patch_client(monkeypatch, fake)
    code, out, err = run(capsys, "stt", "--input", str(wav), "--out-dir", str(tmp_path / "o"))
    assert code == 0 and len(fake.uploads) == 1 and fake.deleted == ["files/SYNTH"]
    assert fake.uploads[0][2].startswith("gaia-stt-") and "big" not in fake.uploads[0][2]  # neutral display name
    part = fake.calls[0][1]["contents"][0]["parts"][0]
    assert part["fileData"]["fileUri"] == "https://x/files/SYNTH"


@needs_ffmpeg
def test_stt_deletes_the_upload_even_when_the_api_fails(capsys, make_wav, tmp_path, monkeypatch):
    from aistudio import ApiError, stt

    wav = make_wav("big.wav", seconds=2.0)
    monkeypatch.setattr(stt, "INLINE_MAX_BYTES", 1000)

    def boom():
        raise ApiError("stt: refused")

    fake = FakeGen(response=boom)
    patch_client(monkeypatch, fake)
    code, _, err = run(capsys, "stt", "--input", str(wav), "--out-dir", str(tmp_path / "o"))
    assert code == 5 and fake.deleted == ["files/SYNTH"]


@needs_ffmpeg
def test_stt_audio_too_long_for_features_is_refused(capsys, make_wav, tmp_path, monkeypatch):
    from aistudio import audio, stt

    wav = make_wav("a.wav", seconds=1.0)
    monkeypatch.setattr(stt, "probe", lambda p: audio.AudioInfo(2400.0, 16000, 1, "pcm_s16le", "s16", 16, 10, "wav"))
    code, _, err = run(capsys, "stt", "--input", str(wav), "--out-dir", str(tmp_path / "o"), "--diarize", "--dry-run")
    assert code == 4 and "at most 30 min" in err


# ---- voices -------------------------------------------------------------------- #
def test_replicate_refuses_without_the_speaker_flag(capsys, make_wav, key_env, tmp_path):
    ref, con = make_wav("r.wav", seconds=12.0), make_wav("c.wav", seconds=5.0)
    code, _, err = run(capsys, "voices", "replicate", "--reference", str(ref), "--consent", str(con), "--lang", "ru-RU",
                       "--alias", "me", "--accept-unpaid-terms", "--dry-run")
    assert code == 2 and "--i-am-the-speaker" in err


@needs_ffmpeg
def test_replicate_dry_run_prints_statement_and_request(capsys, make_wav):
    ref, con = make_wav("r.wav", seconds=12.0), make_wav("c.wav", seconds=5.0)
    code, out, err = run(capsys, "voices", "replicate", "--reference", str(ref), "--consent", str(con),
                         "--lang", "ru-RU", "--alias", "me", "--i-am-the-speaker", "--dry-run")
    assert code == 0 and voices.consent_statement("ru-RU") in out
    assert '"store": false' in out and '"type": "replicated"' in out and "<inline audio:" in out
    assert not config.registry_path().exists() and not config.consent_log_path().exists()


@needs_ffmpeg
def test_replicate_store_flag_is_always_explicit(capsys, make_wav):
    ref, con = make_wav("r.wav", seconds=12.0), make_wav("c.wav", seconds=5.0)
    base = ["voices", "replicate", "--reference", str(ref), "--consent", str(con), "--lang", "ru-RU", "--alias", "me",
            "--i-am-the-speaker", "--dry-run"]
    assert '"store": false' in run(capsys, *base)[1]
    assert '"store": false' in run(capsys, *base, "--no-store")[1]
    assert '"store": true' in run(capsys, *base, "--store")[1]


def test_replicate_with_repo_path_is_refused(capsys, make_wav):
    con = make_wav("c.wav", seconds=5.0)
    code, _, err = run(capsys, "voices", "replicate", "--reference", str(REPO / "assets" / "gaia_comic.png"),
                       "--consent", str(con), "--lang", "ru-RU", "--alias", "me", "--i-am-the-speaker", "--dry-run")
    assert code == 4 and "inside the repository" in err


def test_replicate_needs_paid_ack_before_anything_is_sent(capsys, make_wav, key_env, monkeypatch):
    ref, con = make_wav("r.wav", seconds=12.0), make_wav("c.wav", seconds=5.0)
    patch_client(monkeypatch, FakeGen(), paid=False)
    code, _, err = run(capsys, "voices", "replicate", "--reference", str(ref), "--consent", str(con),
                       "--lang", "ru-RU", "--alias", "me", "--i-am-the-speaker")
    assert code == 2 and "billing-linked" in err


def test_voices_delete_needs_yes_when_not_interactive(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    code, _, err = run(capsys, "voices", "delete", "someone")
    assert code == 2 and "--yes" in err


def test_voices_delete_dry_run_changes_nothing(capsys):
    from aistudio.voices import Registry, VoiceEntry

    reg = Registry(config.registry_path())
    reg.add(VoiceEntry("me", voices.MODEL_FLASH_LITE, "ru-RU", True, "2026-10-19T00:00:00Z", None, voice_id="voice_abc123"))
    code, out, err = run(capsys, "voices", "delete", "me", "--dry-run")
    assert code == 0 and "would delete voice_abc123" in out and "nothing was deleted" in out
    assert reg.get("me") is not None


def test_voices_list_local_empty_and_populated(capsys):
    code, out, _ = run(capsys, "voices", "list", "--local")
    assert code == 0 and "no local aliases" in out
    from aistudio.voices import Registry, VoiceEntry

    Registry(config.registry_path()).add(VoiceEntry("me", voices.MODEL_FLASH_LITE, "ru-RU", False, "2026-10-19T00:00:00Z",
                                                    "2020-01-01T00:00:00Z", voice_key="voicekey_LISTSECRET12345"))
    code, out, err = run(capsys, "voices", "list", "--local")
    assert "me | stateless key | ru-RU" in out and "EXPIRED" in out and "LISTSECRET12345" not in out + err


# ---- redaction at the top level ----------------------------------------------- #
def test_unexpected_exceptions_are_redacted(capsys, monkeypatch, key_env):
    config.register_secret(FAKE_KEY)

    def boom(args):
        raise RuntimeError(f"upstream said: bad key {FAKE_KEY} and voicekey_TOPSECRET123456")

    monkeypatch.setattr(cli, "cmd_consent_text", boom)
    code, out, err = run(capsys, "consent-text", "--lang", "ru-RU")
    assert code == 1 and FAKE_KEY not in out + err and "TOPSECRET123456" not in out + err
    code, out, err = run(capsys, "--verbose", "consent-text", "--lang", "ru-RU")
    assert code == 1 and FAKE_KEY not in out + err and "TOPSECRET123456" not in out + err and "Traceback" in err


def test_nothing_in_the_repo_is_written(capsys, tmp_path, make_wav, monkeypatch):
    before = sorted(p.name for p in REPO.iterdir())
    patch_client(monkeypatch, FakeGen(response=lambda: tts_response(0.2)))
    run(capsys, "tts", "--text", "hello", "--voice", "Kore", "--out-dir", str(tmp_path / "o"))
    run(capsys, "consent-text", "--lang", "ru-RU")
    after = sorted(p.name for p in REPO.iterdir())
    assert before == after
