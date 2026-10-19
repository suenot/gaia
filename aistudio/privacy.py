"""
Privacy guards for the AI Studio speech tools.

GAIA is a PUBLIC repository. Private audio (calls, voice samples) and anything
derived from it (transcripts, synthesized speech) must never end up inside the
repository tree, so:

  * inputs must be ABSOLUTE paths that resolve (symlinks followed) to a place
    OUTSIDE the GAIA checkout (and outside the super-project that embeds it);
  * output directories are chosen by the caller and obey the same rule, which
    also rules out `output/` and `debug/` of this checkout;
  * output file names come from a hash or a caller-supplied slug, never from
    the source file name or a speaker name;
  * output files are created with mode 0600.

There is deliberately no override flag: tests build their synthetic fixtures
in a temp directory outside the checkout.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import AudioError, UsageError

# The GAIA checkout: the directory that contains the `aistudio/` package.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# --------------------------------------------------------------------------- #
# Which trees are off limits
# --------------------------------------------------------------------------- #
def _git_out(*args: str) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(PACKAGE_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = res.stdout.strip()
    return out if res.returncode == 0 and out else None


@functools.lru_cache(maxsize=1)
def forbidden_roots() -> tuple:
    """The GAIA checkout plus, when GAIA is a submodule, the super-project
    working tree that embeds it. Resolved, absolute paths."""
    roots = {PACKAGE_ROOT.resolve()}
    for query in (("rev-parse", "--show-toplevel"), ("rev-parse", "--show-superproject-working-tree")):
        out = _git_out(*query)
        if out:
            roots.add(Path(out).resolve())
    return tuple(sorted(roots))


def is_inside(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------- #
# Path validation
# --------------------------------------------------------------------------- #
def _absolute(raw: str, what: str, error=AudioError) -> Path:
    if not raw or "\x00" in raw:
        raise error(f"{what}: empty or invalid path")
    if raw.startswith("~"):
        raise error(f"{what}: pass an absolute path (expand '~' first)")
    if not os.path.isabs(raw):
        raise error(f"{what}: an absolute path is required, got a relative one")
    return Path(os.path.abspath(raw))  # lexical normalization removes '..'


def _check_outside(lexical: Path, resolved: Path, what: str, roots: Sequence[Path], error) -> None:
    if is_inside(lexical, roots) or is_inside(resolved, roots):
        raise error(
            f"{what}: refused, the path is inside the repository tree. Private audio and "
            "anything derived from it must live outside the repo."
        )


def private_input(raw: str, what: str = "input", roots: Optional[Sequence[Path]] = None) -> Path:
    """Validate an input audio path: absolute, existing regular file, outside
    the repo tree (both as typed and after resolving symlinks)."""
    roots = list(roots) if roots is not None else list(forbidden_roots())
    lexical = _absolute(raw, what)
    resolved = lexical.resolve()
    _check_outside(lexical, resolved, what, roots, AudioError)
    if not resolved.is_file():
        raise AudioError(f"{what}: not a file or does not exist")
    return resolved


def output_dir(raw: str, what: str = "--out-dir", roots: Optional[Sequence[Path]] = None) -> Path:
    """Validate (without creating) an output directory chosen by the caller."""
    roots = list(roots) if roots is not None else list(forbidden_roots())
    lexical = _absolute(raw, what, error=UsageError)
    resolved = lexical.resolve()
    _check_outside(lexical, resolved, what, roots, UsageError)
    if resolved.exists() and not resolved.is_dir():
        raise UsageError(f"{what}: exists and is not a directory")
    return resolved


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def check_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise UsageError("--name: use 1-64 characters from A-Z a-z 0-9 . _ - (first one alphanumeric)")
    return slug


def digest_name(prefix: str, data: bytes, length: int = 12) -> str:
    """Stable output base name derived from content: `<prefix>-<sha256[:12]>`."""
    return f"{prefix}-{hashlib.sha256(data).hexdigest()[:length]}"


def write_private(path: Path, data: bytes, overwrite: bool = False) -> Path:
    """Write `data` to `path` with mode 0600, atomically. Refuses to clobber an
    existing file unless `overwrite`."""
    if path.exists() and not overwrite:
        raise UsageError(f"output file already exists (use --overwrite): {path.name}")
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def describe_text(text: str) -> str:
    """Log-safe description of text: lengths only, never the content."""
    return f"{len(text)} chars, {len(text.split())} words"
