"""Repo-path guard, output-dir policy, 0600 writes."""

import stat

import pytest

from aistudio import AudioError, UsageError, privacy

REPO = privacy.PACKAGE_ROOT


def test_repo_root_is_the_gaia_checkout():
    assert (REPO / "aistudio_speech.py").exists()
    assert REPO.resolve() in privacy.forbidden_roots()


def test_relative_and_tilde_paths_are_refused():
    with pytest.raises(AudioError):
        privacy.private_input("audio/call.wav")
    with pytest.raises(AudioError):
        privacy.private_input("~/call.wav")
    with pytest.raises(AudioError):
        privacy.private_input("")


def test_input_inside_repo_is_refused():
    with pytest.raises(AudioError) as exc:
        privacy.private_input(str(REPO / "README.md"))
    assert "inside the repository" in str(exc.value)
    assert exc.value.exit_code == 4


def test_dotdot_into_repo_is_refused(tmp_path):
    sneaky = f"{tmp_path}/../../../{REPO.relative_to('/')}/README.md"
    with pytest.raises(AudioError):
        privacy.private_input(sneaky)


def test_symlink_pointing_into_repo_is_refused(tmp_path):
    link = tmp_path / "innocent.wav"
    link.symlink_to(REPO / "README.md")
    with pytest.raises(AudioError):
        privacy.private_input(str(link))


def test_input_outside_repo_is_accepted(make_wav):
    wav = make_wav("a.wav")
    assert privacy.private_input(str(wav)) == wav.resolve()


def test_missing_or_directory_input_is_refused(tmp_path):
    with pytest.raises(AudioError):
        privacy.private_input(str(tmp_path / "nope.wav"))
    with pytest.raises(AudioError):
        privacy.private_input(str(tmp_path))


def test_explicit_roots_parameter(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(b"x")
    with pytest.raises(AudioError):
        privacy.private_input(str(f), roots=[tmp_path.resolve()])


@pytest.mark.parametrize("sub", ["output", "debug", "docs", "."])
def test_output_dir_inside_repo_is_refused(sub):
    with pytest.raises(UsageError) as exc:
        privacy.output_dir(str(REPO / sub))
    assert exc.value.exit_code == 2


def test_output_dir_outside_repo_is_accepted_and_not_created(tmp_path):
    target = tmp_path / "new" / "dir"
    assert privacy.output_dir(str(target)) == target.resolve()
    assert not target.exists()


def test_output_dir_that_is_a_file_is_refused(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(UsageError):
        privacy.output_dir(str(f))


def test_write_private_mode_and_no_clobber(tmp_path):
    path = tmp_path / "out.txt"
    privacy.write_private(path, b"one")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(UsageError):
        privacy.write_private(path, b"two")
    privacy.write_private(path, b"two", overwrite=True)
    assert path.read_bytes() == b"two"
    assert not list(tmp_path.glob("*.part"))


def test_ensure_private_dir_mode(tmp_path):
    d = privacy.ensure_private_dir(tmp_path / "x")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_slug_and_digest_names():
    assert privacy.check_slug("call-2026_10.19") == "call-2026_10.19"
    for bad in ("", "../x", "a/b", "-x", "a b", "x" * 65, "имя"):
        with pytest.raises(UsageError):
            privacy.check_slug(bad)
    a = privacy.digest_name("tts", b"hello")
    assert a == privacy.digest_name("tts", b"hello") and a.startswith("tts-") and len(a) == 16
    assert a != privacy.digest_name("tts", b"hello!")


def test_describe_text_has_no_content():
    desc = privacy.describe_text("секретный текст звонка")
    assert "секретный" not in desc and "chars" in desc
