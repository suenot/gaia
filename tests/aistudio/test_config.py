"""Key loading, the 0600 rule, and secret redaction."""

import os

import pytest

from aistudio import AuthError, config, privacy
from conftest import FAKE_KEY


def write_key_file(cfg, content=f"GEMINI_API_KEY={FAKE_KEY}\n", mode=0o600):
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / config.KEY_FILE_NAME
    path.write_text(content)
    os.chmod(path, mode)
    return path


def test_no_key_raises_auth_error(isolated_env):
    with pytest.raises(AuthError) as exc:
        config.load_settings()
    assert exc.value.exit_code == 3
    assert "GEMINI_API_KEY" in str(exc.value)


def test_env_key_wins_over_file(isolated_env, monkeypatch):
    write_key_file(isolated_env, "GEMINI_API_KEY=from-the-file-0000\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from-the-env-111111")
    s = config.load_settings()
    assert s.key_source == "env" and s.api_key == "from-the-env-111111"


def test_key_file_0600_is_read(isolated_env):
    write_key_file(isolated_env)
    s = config.load_settings()
    assert s.key_source == "file" and s.api_key == FAKE_KEY


def test_key_file_0400_is_read(isolated_env):
    write_key_file(isolated_env, mode=0o400)
    assert config.load_settings().api_key == FAKE_KEY


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o700])
def test_key_file_with_group_or_other_bits_is_refused(isolated_env, mode):
    write_key_file(isolated_env, mode=mode)
    with pytest.raises(AuthError) as exc:
        config.load_settings()
    assert "chmod 600" in str(exc.value)
    assert FAKE_KEY not in str(exc.value)


def test_key_file_symlink_to_loose_target_is_refused(isolated_env, tmp_path):
    target = tmp_path / "elsewhere.env"
    target.write_text(f"GEMINI_API_KEY={FAKE_KEY}\n")
    os.chmod(target, 0o644)
    isolated_env.mkdir(parents=True)
    (isolated_env / config.KEY_FILE_NAME).symlink_to(target)
    with pytest.raises(AuthError):
        config.load_settings()


def test_key_file_inside_repo_is_refused(isolated_env, tmp_path):
    write_key_file(isolated_env)
    with pytest.raises(AuthError) as exc:
        config.load_settings(roots=[isolated_env.parent])  # pretend tmp is the repo
    assert "repository" in str(exc.value)


def test_file_without_key_is_refused(isolated_env):
    write_key_file(isolated_env, "OTHER=1\n")
    with pytest.raises(AuthError):
        config.load_settings()


def test_blank_or_spaced_env_key_is_refused(isolated_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "two words")
    with pytest.raises(AuthError):
        config.load_settings()


def test_paid_flag_from_env_and_file(isolated_env, monkeypatch):
    write_key_file(isolated_env, f"GEMINI_API_KEY={FAKE_KEY}\nGAIA_AISTUDIO_PAID_PROJECT=1\n")
    assert config.load_settings().paid_project is True
    path = isolated_env / config.KEY_FILE_NAME
    path.write_text(f"GEMINI_API_KEY={FAKE_KEY}\n")
    assert config.load_settings().paid_project is False
    monkeypatch.setenv("GAIA_AISTUDIO_PAID_PROJECT", "yes")
    assert config.load_settings().paid_project is True


def test_parse_env_text_forms():
    text = (
        "# comment\n\nexport A=1\nB = \"two words\"\nC='x' # trailing\nD=plain # note\nnot a pair\n"
    )
    assert config.parse_env_text(text) == {"A": "1", "B": "two words", "C": "x", "D": "plain"}


def test_config_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("GAIA_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_dir() == tmp_path / "xdg" / "gaia"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config.config_dir() == config.Path.home() / ".config" / "gaia"


def test_config_paths_are_outside_the_repo(isolated_env):
    for path in (config.registry_path(), config.consent_log_path()):
        assert not privacy.is_inside(path.resolve(), privacy.forbidden_roots())


# ---- redaction ----------------------------------------------------------- #
def test_settings_repr_never_shows_the_key(isolated_env):
    s = config.Settings(api_key=FAKE_KEY, key_source="env", config_dir=isolated_env)
    assert FAKE_KEY not in repr(s) and FAKE_KEY not in str(s)


def test_redact_registered_secret_and_patterns():
    config.register_secret("custom-secret-value-xyz")
    msg = (
        f"boom custom-secret-value-xyz and {FAKE_KEY} and voicekey_AbCdEf123456_-+/= "
        "and https://x/y?alt=json&key=SOMETHING123 and x-goog-api-key: abcdef1234567890 "
        "and Authorization: Bearer ya29.a0AbCdEf1234567890 and GEMINI_API_KEY=supersecretvalue"
    )
    out = config.redact(msg)
    for leaked in ("custom-secret-value-xyz", FAKE_KEY, "AbCdEf123456", "SOMETHING123", "abcdef1234567890",
                   "ya29.a0AbCdEf", "supersecretvalue"):
        assert leaked not in out
    assert "<redacted>" in out


def test_loading_a_key_registers_it_for_redaction(isolated_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "an-unusual-shaped-key-0042")
    config.load_settings()
    assert "an-unusual-shaped-key-0042" not in config.redact("failed with an-unusual-shaped-key-0042")


def test_auth_messages_do_not_contain_secrets(isolated_env):
    write_key_file(isolated_env, mode=0o644)
    try:
        config.load_settings()
    except AuthError as exc:
        assert FAKE_KEY not in config.redact(str(exc))
