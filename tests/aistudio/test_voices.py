"""Consent table, local registry, voice resolution, deletion."""

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from aistudio import AuthError, UsageError, voices
from conftest import FIXTURES

RU = "Я являюсь владельцем этого голоса и даю согласие Google на использование этого голоса для создания модели синтетического голоса."
EN = "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model."


def fixture_statements():
    return json.loads((FIXTURES / "consent_statements.json").read_text(encoding="utf-8"))["statements"]


def test_consent_table_equals_the_published_statements_byte_for_byte():
    expected = fixture_statements()
    assert len(expected) == 30
    assert {loc: stmt for loc, (_, stmt) in voices.CONSENT_STATEMENTS.items()} == expected
    for loc, stmt in expected.items():
        assert voices.consent_statement(loc).encode("utf-8") == stmt.encode("utf-8")


def test_russian_and_english_statements_are_literal():
    assert voices.consent_statement("ru-RU") == RU
    for loc in ("en-US", "en-GB", "en-IN", "en-AU"):
        assert voices.consent_statement(loc) == EN


def test_locale_normalization():
    assert voices.normalize_lang("ru-ru") == "ru-RU"
    assert voices.normalize_lang("ru_RU") == "ru-RU"
    assert voices.normalize_lang("ru") == "ru-RU"
    assert voices.normalize_lang("DE-de") == "de-DE"
    with pytest.raises(UsageError) as exc:
        voices.normalize_lang("en")  # four English variants
    assert "ambiguous" in str(exc.value)
    with pytest.raises(UsageError):
        voices.normalize_lang("xx-YY")


def test_consent_text_block_contains_statement_and_checklist():
    block = voices.consent_text_block("ru-RU")
    assert RU in block and "ru-RU" in block
    assert "Only your own voice" in block and "same microphone" in block


def test_prebuilt_voice_list_matches_the_docs():
    assert len(voices.PREBUILT_VOICES) == 30 and len(set(voices.PREBUILT_VOICES)) == 30
    assert {"Kore", "Puck", "Zephyr", "Sulafat", "Vindemiatrix"} <= set(voices.PREBUILT_VOICES)


def test_model_aliases():
    assert voices.resolve_model("flash") == "gemini-3.8-flash-tts"
    assert voices.resolve_model("flash-lite") == "gemini-3.8-flash-lite-tts"
    assert voices.resolve_model("gemini-9-x") == "gemini-9-x"


# ---- registry ---------------------------------------------------------------- #
def entry(alias="me", store=False, **kw):
    base = dict(alias=alias, model=voices.MODEL_FLASH_LITE, language_code="ru-RU", store=store,
                created="2026-10-19T00:00:00Z", expires="2099-01-01T00:00:00Z")
    if store:
        base["voice_id"] = "voice_abc123"
    else:
        base["voice_key"] = "voicekey_SYNTHETIC0123"
    base.update(kw)
    return voices.VoiceEntry(**base)


def test_registry_roundtrip_and_file_mode(tmp_path):
    reg = voices.Registry(tmp_path / "cfg" / "voices.json")
    assert reg.all() == [] and reg.get("me") is None
    reg.add(entry("me"))
    reg.add(entry("stored1", store=True))
    assert stat.S_IMODE(reg.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(reg.path.parent.stat().st_mode) == 0o700
    assert [e.alias for e in reg.all()] == ["me", "stored1"]
    assert reg.get("me").value == "voicekey_SYNTHETIC0123"
    assert reg.get("stored1").value == "voice_abc123"
    assert reg.find_by_id("voice_abc123").alias == "stored1"
    assert reg.remove("me") and not reg.remove("me")
    assert not list(reg.path.parent.glob(".voices-*"))


def test_registry_refuses_loose_permissions(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    reg.add(entry("me"))
    os.chmod(reg.path, 0o644)
    with pytest.raises(AuthError):
        reg.all()


def test_registry_alias_rules(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    reg.add(entry("me"))
    with pytest.raises(UsageError):
        reg.add(entry("me"))  # collision
    for bad in ("Me", "-x", "a b", "x" * 33, "kore"):  # 'kore' collides with prebuilt Kore
        with pytest.raises(UsageError):
            reg.check_alias_free(bad)
    reg.check_alias_free("fresh-one")


def test_registry_repr_hides_the_key():
    assert "SYNTHETIC0123" not in repr(entry("me"))


def test_registry_rejects_corrupt_json(tmp_path):
    path = tmp_path / "voices.json"
    path.write_text("{not json")
    os.chmod(path, 0o600)
    with pytest.raises(AuthError):
        voices.Registry(path).all()


# ---- resolving --voice --------------------------------------------------------- #
def test_resolve_prebuilt_raw_ids_and_library_names(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    assert voices.resolve_voice("Kore", reg) == voices.ResolvedVoice("Kore", "prebuilt", "Kore", False)
    r = voices.resolve_voice("voice_xyz", reg)
    assert (r.kind, r.custom) == ("stored", True)
    k = voices.resolve_voice("voicekey_SECRETSECRET", reg)
    assert k.kind == "key" and "SECRETSECRET" not in k.label  # label is log-safe
    lib = voices.resolve_voice("SomeLibraryVoice", reg)
    assert lib.kind == "library" and "unverified" in lib.label
    with pytest.raises(UsageError):
        voices.resolve_voice("  ", reg)


def test_resolve_alias_and_expiry(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    reg.add(entry("fresh"))
    reg.add(entry("old", expires="2020-01-01T00:00:00Z"))
    r = voices.resolve_voice("fresh", reg)
    assert r.value == "voicekey_SYNTHETIC0123" and r.kind == "alias-key" and "SYNTHETIC" not in r.label
    with pytest.raises(UsageError) as exc:
        voices.resolve_voice("old", reg)
    assert "expired" in str(exc.value)


def test_parse_time_variants():
    assert voices.parse_time("2026-10-26T00:00:00Z") == datetime(2026, 10, 26, tzinfo=timezone.utc)
    assert voices.parse_time("2026-10-26T00:00:00.123456789Z").microsecond == 123456
    assert voices.parse_time("garbage") is None and voices.parse_time(None) is None
    assert voices.iso(datetime(2026, 10, 26, tzinfo=timezone.utc)) == "2026-10-26T00:00:00Z"


# ---- listing and deleting ---------------------------------------------------- #
class FakeClient:
    def __init__(self):
        self.deleted = []
        self.pages = []
        self.calls = []

    def delete_voice(self, voice_id):
        self.deleted.append(voice_id)

    def list_voices(self, **kw):
        self.calls.append(kw)
        return self.pages.pop(0)


def test_delete_stored_alias_removes_remote_and_local(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    reg.add(entry("s", store=True))
    fake = FakeClient()
    actions = voices.delete_voice(fake, reg, "s")
    assert fake.deleted == ["voice_abc123"] and reg.get("s") is None
    assert any("deleted voice_abc123" in a for a in actions)


def test_delete_stateless_alias_is_local_only(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    reg.add(entry("k"))
    fake = FakeClient()
    actions = voices.delete_voice(fake, reg, "k")
    assert fake.deleted == [] and reg.get("k") is None
    assert any("7 day" in a for a in actions)


def test_delete_raw_id_and_unknown(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    fake = FakeClient()
    voices.delete_voice(fake, reg, "voice_zzz")
    assert fake.deleted == ["voice_zzz"]
    with pytest.raises(UsageError):
        voices.delete_voice(fake, reg, "nothing-here")


def test_list_remote_pagination_and_row_formatting():
    fake = FakeClient()
    fake.pages = [
        {"voices": [{"id": "Kore", "type": "prebuilt", "language_code": "ru-RU", "gender": "female"}], "next_page_token": "t"},
        {"voices": [{"id": "voice_a", "type": "replicated", "expire_time": "2027-01-01T00:00:00Z", "key": "voicekey_LEAK1234"}]},
    ]
    rows = voices.list_remote(fake, voices.list_filters(lang="ru-RU"), fetch_all=True)
    assert len(rows) == 2 and fake.calls[1]["page_token"] == "t"
    line = voices.format_voice_row(rows[0])
    assert "Kore" in line and "ru-RU" in line
    assert "LEAK1234" not in voices.format_voice_row(rows[1]) and "expires 2027" in voices.format_voice_row(rows[1])


def test_plan_delete_describes_without_acting(tmp_path):
    reg = voices.Registry(tmp_path / "voices.json")
    reg.add(entry("s", store=True))
    reg.add(entry("k"))
    assert "would delete voice_abc123 at Google" in voices.plan_delete(reg, "s")[0]
    assert "stateless key" in voices.plan_delete(reg, "k")[0]
    assert voices.plan_delete(reg, "voice_zzz") == ["would delete voice_zzz at Google"]
    with pytest.raises(UsageError):
        voices.plan_delete(reg, "nothing")
    assert reg.get("s") is not None and reg.get("k") is not None  # untouched
