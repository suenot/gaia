"""
End-to-end through the REAL google-genai SDK with a mocked HTTP transport:
what actually goes on the wire, how responses are parsed, how errors map.
No network, no key (a fake one is used), synthetic audio.
"""

import base64
import json
import re

import httpx
import pytest

pytest.importorskip("google.genai")

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from aistudio import ApiError, AuthError, config, stt, tts, voice_clone, voices  # noqa: E402
from aistudio.client import API_GENERATE_CONTENT, API_INTERACTIONS, GeminiClient, map_error  # noqa: E402
from conftest import FAKE_KEY, b64, load_fixture, sine_wav_bytes  # noqa: E402

GC, IX = API_GENERATE_CONTENT, API_INTERACTIONS
LITE = voices.MODEL_FLASH_LITE


class Wire:
    """Scripted HTTP server: queue (status, json) answers; records requests."""

    def __init__(self):
        self.requests = []
        self.answers = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers.pop(0) if self.answers else (200, {})
        return httpx.Response(status, json=body)

    def client(self, api=GC):
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        sdk = genai.Client(
            api_key=FAKE_KEY,
            http_options=types.HttpOptions(httpx_client=http, retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[599])),
        )
        return GeminiClient(sdk=sdk, api=api, sleep=lambda _s: None, http=http, api_key=FAKE_KEY)

    def last_json(self):
        return json.loads(self.requests[-1].content)


@pytest.fixture
def wire():
    return Wire()


def camel(key: str) -> str:
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), key)


def _bytes(text: str) -> bytes:
    """Proto JSON accepts both base64 alphabets; the SDK sends URL-safe."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def norm(obj):
    """Wire JSON may use snake_case or camelCase and either base64 alphabet
    (proto JSON accepts both)."""
    if isinstance(obj, dict):
        return {camel(k): (_bytes(v) if k == "data" and isinstance(v, str) else norm(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [norm(v) for v in obj]
    return obj


def tts_ok_response(seconds=0.2):
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"inlineData": {"mimeType": "audio/wav", "data": b64(sine_wav_bytes(seconds))}}]}, "finishReason": "STOP"}]}


# ---- what goes on the wire ------------------------------------------------- #
def test_tts_generate_content_wire_matches_builder(wire):
    client = wire.client(GC)
    wire.answers.append((200, tts_ok_response()))
    req = tts.build_speech_request(LITE, "Привет, мир.", "Kore", style="calm", lang="ru-RU", api=GC)
    client.generate(req.model, req.body, "tts")
    r = wire.requests[-1]
    assert r.method == "POST" and r.url.path == f"/v1beta/models/{LITE}:generateContent"
    sent = norm(wire.last_json())
    assert sent["contents"] == norm(req.body["contents"])
    assert sent["generationConfig"] == norm(req.body["generationConfig"])


def test_stt_generate_content_wire_matches_builder_with_inline_audio(wire):
    client = wire.client(GC)
    wire.answers.append((200, load_fixture("stt_generate_content_diarized.json")))
    audio = stt.AudioPart("audio/wav", data_b64=b64(sine_wav_bytes(0.1)))
    req = stt.build_transcribe_request(audio, stt.SttOptions(language_codes=["ru-RU"], diarize=True, words=True), api=GC)
    client.generate(req.model, req.body, "stt")
    sent = norm(wire.last_json())
    assert sent["contents"] == norm(req.body["contents"])  # inline base64 survives the SDK round trip
    assert sent["generationConfig"] == norm(req.body["generationConfig"])
    assert wire.requests[-1].url.path == "/v1beta/models/gemini-3.5-transcribe:generateContent"


def test_stt_empty_language_list_still_sends_a_transcription_config(wire):
    client = wire.client(GC)
    wire.answers.append((200, load_fixture("stt_generate_content_plain.json")))
    req = stt.build_transcribe_request(stt.AudioPart("audio/wav", uri="https://x/files/s"), stt.SttOptions(), api=GC)
    client.generate(req.model, req.body, "stt")
    assert "audioTranscriptionConfig" in norm(wire.last_json())["generationConfig"]


def test_interactions_wire_is_stateless_and_matches_builder(wire):
    client = wire.client(IX)
    wire.answers.append((200, {"status": "completed", "steps": []}))
    req = tts.build_speech_request(LITE, "Hello.", "Kore", style="calm", api=IX)
    client.generate(req.model, req.body, "tts")
    r = wire.requests[-1]
    sent = wire.last_json()
    assert r.url.path == "/v1beta/interactions" and sent["store"] is False
    assert sent["model"] == LITE and sent["input"] == req.body["input"]
    assert sent["response_format"] == {"type": "audio"}
    assert sent["generation_config"] == req.body["generation_config"]


def test_interactions_stt_wire(wire):
    client = wire.client(IX)
    wire.answers.append((200, load_fixture("stt_interactions_diarized.json")))
    req = stt.build_transcribe_request(
        stt.AudioPart("audio/wav", uri="https://x/files/s"), stt.SttOptions(diarize=True, words=True), api=IX)
    client.generate(req.model, req.body, "stt")
    sent = wire.last_json()
    assert sent["generation_config"] == req.body["generation_config"]
    assert sent["store"] is False
    assert sent["input"][0]["content"][0] == req.body["input"][0]  # the SDK wraps it as a user_input step


def test_create_voice_wire_matches_documented_json(wire):
    client = wire.client()
    wire.answers.append((200, {"id": "voice_SYNTH123", "type": "replicated", "model": LITE,
                               "expire_time": "2027-10-19T00:00:00Z"}))
    body = voice_clone.build_create_request(LITE, True, b64(b"AAA"), b64(b"BBB"), display_name="me")
    resp = client.create_voice(body)
    r = wire.requests[-1]
    assert r.method == "POST" and r.url.path == "/v1beta/voices"
    assert wire.last_json() == body
    assert voice_clone.parse_create_response(resp, True)[0] == "voice_SYNTH123"


def test_create_voice_stateless_returns_key_and_is_never_retried(wire):
    client = wire.client()
    wire.answers += [(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}),
                     (200, {"key": "voicekey_SYNTH", "type": "replicated"})]
    body = voice_clone.build_create_request(LITE, False, b64(b"AAA"), b64(b"BBB"))
    with pytest.raises(ApiError):
        client.create_voice(body)
    assert len(wire.requests) == 1  # no retry: a retry could create a second voice
    wire.answers.append((200, {"key": "voicekey_SYNTH", "type": "replicated"}))
    resp = client.create_voice(body)
    assert wire.last_json()["store"] is False
    assert voice_clone.parse_create_response(resp, False)[1] == "voicekey_SYNTH"


def test_create_voice_requires_explicit_store(wire):
    with pytest.raises(ValueError):
        wire.client().create_voice({"voice": {"type": "replicated"}})


def test_list_get_delete_voices_wire(wire):
    client = wire.client()
    wire.answers.append((200, {"voices": [{"id": "Kore", "type": "prebuilt", "language_code": "ru-RU"}],
                               "next_page_token": "t2"}))
    rows = voices.list_remote(client, voices.list_filters(lang="ru-RU", type_="prebuilt", page_size=5))
    url = wire.requests[-1].url
    assert url.path == "/v1beta/voices"
    assert dict(url.params) == {"language_code": "ru-RU", "type": "prebuilt", "page_size": "5"}
    assert rows[0]["id"] == "Kore"
    wire.answers.append((200, {"id": "voice_a", "type": "replicated"}))
    assert client.get_voice("voice_a")["id"] == "voice_a"
    assert wire.requests[-1].url.path == "/v1beta/voices/voice_a" and wire.requests[-1].method == "GET"
    wire.answers.append((200, {}))
    client.delete_voice("voice_a")
    assert wire.requests[-1].method == "DELETE" and wire.requests[-1].url.path == "/v1beta/voices/voice_a"


def test_list_all_follows_pagination(wire):
    client = wire.client()
    wire.answers += [(200, {"voices": [{"id": "A"}], "next_page_token": "t2"}), (200, {"voices": [{"id": "B"}]})]
    rows = voices.list_remote(client, voices.list_filters(), fetch_all=True)
    assert [r["id"] for r in rows] == ["A", "B"]
    assert dict(wire.requests[-1].url.params)["page_token"] == "t2"


def test_key_only_in_the_api_key_header_never_in_url_or_body(wire):
    client = wire.client()
    wire.answers += [(200, tts_ok_response()), (200, {"voices": []}), (200, {"id": "v", "type": "replicated"})]
    req = tts.build_speech_request(LITE, "Hi.", "Kore")
    client.generate(req.model, req.body, "tts")
    client.list_voices()
    client.create_voice(voice_clone.build_create_request(LITE, True, b64(b"A"), b64(b"B")))
    for r in wire.requests:
        assert r.headers["x-goog-api-key"] == FAKE_KEY
        assert FAKE_KEY not in str(r.url) and FAKE_KEY.encode() not in r.content
        assert "authorization" not in {k.lower() for k in r.headers}


def test_vertex_mode_is_forced_off(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "some-other-key-value")
    s = config.Settings(api_key=FAKE_KEY, key_source="env", config_dir=config.Path("/nonexistent"))
    sdk = GeminiClient(s).sdk
    assert sdk.vertexai is False


# ---- parsing responses ------------------------------------------------------ #
def test_tts_generate_content_response_roundtrip(wire):
    client = wire.client(GC)
    wire.answers.append((200, tts_ok_response(0.5)))
    voice = voices.resolve_voice("Kore")
    result = tts.synthesize(client, "Hello there.", voice, model="flash-lite")
    assert result.requests == 1 and not result.stitched
    assert result.duration_s == pytest.approx(0.5, abs=0.01)
    assert result.wav[:4] == b"RIFF"


def test_tts_interactions_response_roundtrip(wire):
    client = wire.client(IX)
    wav = sine_wav_bytes(0.4)
    wire.answers.append((200, {"status": "completed", "steps": [
        {"type": "model_output", "content": [{"type": "audio", "data": b64(wav), "mime_type": "audio/wav"}]}]}))
    result = tts.synthesize(client, "Hello there.", voices.resolve_voice("Kore"), model="flash")
    assert result.wav == wav


def test_tts_long_text_is_chunked_stitched_and_flagged(wire):
    client = wire.client(GC)
    text = " ".join(["Это предложение номер один."] * 40)  # > 200 chars
    n_chunks = len(tts.chunk_text(text, 200)) if hasattr(tts, "chunk_text") else None
    wire.answers += [(200, tts_ok_response(0.3)) for _ in range(20)]
    result = tts.synthesize(client, text, voices.resolve_voice("Kore"), model="flash-lite", max_chars=200, gap_ms=100)
    assert result.requests == len(wire.requests) > 1 and result.stitched
    assert result.duration_s == pytest.approx(0.3 * result.requests + 0.1 * (result.requests - 1), abs=0.02)
    del n_chunks


def test_dialogue_is_synthesized_turn_by_turn(wire):
    client = wire.client(GC)
    wire.answers += [(200, tts_ok_response(0.2)) for _ in range(3)]
    turns = tts.parse_turns("A: One.\nB: Two.\nA: Three.", ["A", "B"])
    voice_map = {"A": voices.resolve_voice("Kore"), "B": voices.resolve_voice("voice_custom1")}
    result = tts.synthesize_dialogue(client, turns, voice_map, model="flash-lite", gap_ms=0)
    assert result.requests == 3
    used = [norm(json.loads(r.content))["generationConfig"]["speechConfig"]["voiceConfig"]["voice"] for r in wire.requests]
    assert used == ["Kore", "voice_custom1", "Kore"]


def test_no_audio_response_is_an_api_error(wire):
    client = wire.client(GC)
    wire.answers.append((200, {"candidates": [{"finishReason": "SAFETY"}]}))
    with pytest.raises(ApiError) as exc:
        tts.synthesize(client, "Hello.", voices.resolve_voice("Kore"))
    assert "SAFETY" in str(exc.value) and exc.value.exit_code == 5


def test_stt_transcribe_file_inline_generate_content(wire, make_wav):
    client = wire.client(GC)
    wire.answers.append((200, load_fixture("stt_generate_content_diarized.json")))
    wav = make_wav("s.wav", seconds=1.0)
    from aistudio import audio

    prepared = stt.PreparedAudio(wav, "audio/wav", audio.AudioInfo(1.0, 24000, 1, "pcm_s16le", "s16", 16, wav.stat().st_size, "wav"), False)
    tr, meta = stt.transcribe_file(client, prepared, stt.SttOptions(language_codes=["ru-RU"], diarize=True, words=True))
    assert tr.speakers == ["spk_1", "spk_2"] and meta["transport"] == "inline"
    assert [w.text for w in tr.words][:3] == ["Привет,", "как", "дела?"]
    assert tr.words[3].speaker == "spk_2" and tr.words[3].start == pytest.approx(2.5)


def test_stt_interactions_parsing(wire):
    tr = stt.parse_interactions(load_fixture("stt_interactions_diarized.json"))
    assert tr.speakers == ["spk_1", "spk_2"]
    assert [s.text for s in tr.segments] == ["Привет, как дела?", "Хорошо, спасибо."]
    plain = stt.parse_interactions(load_fixture("stt_interactions_plain.json"))
    assert plain.text == "Привет, как дела?" and plain.speakers == []


# ---- errors ------------------------------------------------------------------ #
def err(code, status, message):
    return (code, {"error": {"code": code, "message": message, "status": status}})


def test_region_block_maps_to_api_error_with_hint(wire):
    client = wire.client(GC)
    wire.answers.append(err(400, "FAILED_PRECONDITION", "User location is not supported for the API use."))
    with pytest.raises(ApiError) as exc:
        client.create_voice(voice_clone.build_create_request(LITE, False, b64(b"A"), b64(b"B")))
    assert exc.value.exit_code == 5 and "region" in str(exc.value).lower()


@pytest.mark.parametrize("answer", [
    err(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
    err(401, "UNAUTHENTICATED", "Request had invalid authentication credentials."),
])
def test_bad_key_maps_to_exit_3(wire, answer):
    client = wire.client(GC)
    wire.answers.append(answer)
    with pytest.raises(AuthError) as exc:
        client.list_voices()
    assert exc.value.exit_code == 3


def test_quota_and_permission_and_not_found(wire):
    client = wire.client(GC)
    wire.answers.append(err(429, "RESOURCE_EXHAUSTED", "Quota exceeded"))
    with pytest.raises(ApiError) as exc:
        client.list_voices()
    assert "quota" in str(exc.value).lower()
    wire.answers.append(err(403, "PERMISSION_DENIED", "The caller does not have permission"))
    with pytest.raises(ApiError) as exc:
        client.list_voices()
    assert "permission" in str(exc.value).lower()
    wire.answers.append(err(404, "NOT_FOUND", "model not found"))
    with pytest.raises(ApiError) as exc:
        req = tts.build_speech_request("gemini-9-nope", "Hi.", "Kore")
        client.generate(req.model, req.body, "tts")
    assert "not found" in str(exc.value).lower()


def test_transient_errors_are_retried_then_succeed(wire):
    client = wire.client(GC)
    wire.answers += [err(503, "UNAVAILABLE", "overloaded"), err(500, "INTERNAL", "oops"), (200, tts_ok_response())]
    result = tts.synthesize(client, "Hello.", voices.resolve_voice("Kore"))
    assert len(wire.requests) == 3 and result.requests == 1


def test_persistent_server_error_gives_up_after_three_attempts(wire):
    client = wire.client(GC)
    wire.answers += [err(503, "UNAVAILABLE", "overloaded")] * 5
    with pytest.raises(ApiError):
        client.list_voices()
    assert len(wire.requests) == 3


def test_client_errors_are_not_retried(wire):
    client = wire.client(GC)
    wire.answers += [err(400, "INVALID_ARGUMENT", "bad field")] * 3
    with pytest.raises(ApiError):
        client.list_voices()
    assert len(wire.requests) == 1


def test_network_error_maps_to_api_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(transport=httpx.MockTransport(boom))
    sdk = genai.Client(api_key=FAKE_KEY, http_options=types.HttpOptions(
        httpx_client=http, retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[599])))
    client = GeminiClient(sdk=sdk, sleep=lambda _s: None)
    with pytest.raises(ApiError) as exc:
        client.list_voices()
    assert "network" in str(exc.value)


def test_server_message_echoing_the_key_is_redacted(wire):
    config.register_secret(FAKE_KEY)
    client = wire.client(GC)
    wire.answers.append(err(400, "INVALID_ARGUMENT", f"bad request, key={FAKE_KEY} and voicekey_SECRETSECRET12345"))
    with pytest.raises(ApiError) as exc:
        client.list_voices()
    text = str(exc.value)
    assert FAKE_KEY not in text and "SECRETSECRET12345" not in text


def test_map_error_passes_through_our_own_errors():
    e = ApiError("x")
    assert map_error(e) is e


# ---- Files API (resumable upload) --------------------------------------------- #
def test_upload_never_sends_the_local_file_name_and_delete_follows(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cmd = request.headers.get("x-goog-upload-command", "")
        if cmd == "start":
            return httpx.Response(200, json={}, headers={
                "x-goog-upload-url": "https://generativelanguage.googleapis.com/upload/v1beta/files/SESSION",
                "x-goog-upload-status": "active"})
        if "finalize" in cmd:
            return httpx.Response(200, headers={"x-goog-upload-status": "final"}, json={"file": {
                "name": "files/abc123", "uri": "https://generativelanguage.googleapis.com/v1beta/files/abc123",
                "mimeType": "audio/wav", "state": "ACTIVE", "displayName": "gaia-stt-1"}})
        return httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sdk = genai.Client(api_key=FAKE_KEY, http_options=types.HttpOptions(
        httpx_client=http, retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[599])))
    client = GeminiClient(sdk=sdk, sleep=lambda _s: None)
    secret_name = tmp_path / "Ivan-Petrov-private-call.wav"
    secret_name.write_bytes(b"RIFF" + b"0" * 4000)

    uploaded = client.upload_file(str(secret_name), "audio/wav", display_name="gaia-stt-1")
    assert uploaded.name == "files/abc123" and uploaded.uri.endswith("/files/abc123")
    assert client.delete_file(uploaded.name) is True

    everything = " ".join(
        f"{r.method} {r.url} {dict(r.headers)} {r.content[:300]!r}" for r in seen
    )
    assert "Ivan" not in everything and "Petrov" not in everything and "private-call" not in everything
    assert seen[-1].method == "DELETE" and seen[-1].url.path == "/v1beta/files/abc123"


def test_delete_file_failure_returns_false_instead_of_raising(wire):
    client = wire.client()
    wire.answers.append(err(404, "NOT_FOUND", "file gone"))
    assert client.delete_file("files/zzz") is False
