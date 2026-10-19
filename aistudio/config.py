"""
Key and path resolution for the AI Studio speech tools, plus secret redaction.

Key lookup order:
  1. environment variable GEMINI_API_KEY;
  2. the file  ${XDG_CONFIG_HOME:-~/.config}/gaia/aistudio.env  containing a
     line  GEMINI_API_KEY=...   The file is refused unless it is a regular file
     owned by you with mode 0600 (the stricter 0400 also passes)
     and it does not live inside the repository tree.

The key is never printed, logged or written anywhere by GAIA. Every message
that might carry it goes through `redact()`.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from . import AuthError
from . import privacy

ENV_KEY = "GEMINI_API_KEY"
ENV_CONFIG_DIR = "GAIA_CONFIG_DIR"  # explicit override (also used by the tests)
ENV_PAID_FLAG = "GAIA_AISTUDIO_PAID_PROJECT"  # "1": the key belongs to a billing-linked project
KEY_FILE_NAME = "aistudio.env"
REGISTRY_FILE_NAME = "voices.json"
CONSENT_LOG_FILE_NAME = "voice-consent.jsonl"

REDACTED = "<redacted>"

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
_EXTRA_SECRETS: set = set()

_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{16,}"), REDACTED),
    (re.compile(r"voicekey_[0-9A-Za-z_\-+/=.]{6,}"), "voicekey_" + REDACTED),
    (re.compile(r"(?i)(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)([?&]key=)[^&\s'\"]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1" + REDACTED),
    (re.compile(r"(?i)(GEMINI_API_KEY\s*=\s*)[^\s'\"]{8,}"), r"\1" + REDACTED),
]


def register_secret(value: Optional[str]) -> None:
    """Remember a secret so `redact()` can scrub it even if it has no
    recognizable shape."""
    if value and len(value) >= 6:
        _EXTRA_SECRETS.add(value)


def redact(text: str) -> str:
    out = str(text)
    for secret in sorted(_EXTRA_SECRETS, key=len, reverse=True):
        out = out.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def config_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get(ENV_CONFIG_DIR)
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "gaia"


def registry_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return config_dir(env) / REGISTRY_FILE_NAME


def consent_log_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return config_dir(env) / CONSENT_LOG_FILE_NAME


# --------------------------------------------------------------------------- #
# Secret-file checks
# --------------------------------------------------------------------------- #
def check_private_file(path: Path, what: str) -> None:
    """Raise AuthError unless `path` is a regular file, owned by the current
    user, with mode exactly 0600 (0400 is accepted too: it is stricter)."""
    try:
        st = path.stat()
    except OSError as exc:
        raise AuthError(f"{what}: cannot stat the file ({exc.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise AuthError(f"{what}: not a regular file")
    if st.st_uid != os.getuid():
        raise AuthError(f"{what}: not owned by the current user")
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
        raise AuthError(
            f"{what}: permissions are {stat.S_IMODE(st.st_mode):04o}, refusing to read it. "
            f"Run: chmod 600 {path}"
        )


def parse_env_text(text: str) -> dict:
    """Minimal dotenv parser: KEY=VALUE lines, optional `export `, quotes,
    comments. No variable expansion."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        quoted = re.match(r"""^(['"])(.*?)\1\s*(?:#.*)?$""", value)
        if quoted:
            value = quoted.group(2)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[name.strip()] = value
    return values


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    key_source: str  # "env" or "file"
    config_dir: Path
    paid_project: bool = False

    def __repr__(self) -> str:  # never show the key
        return (
            f"Settings(key_source={self.key_source!r}, config_dir={str(self.config_dir)!r}, "
            f"paid_project={self.paid_project})"
        )


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_key(value: str) -> str:
    value = value.strip()
    if not value or re.search(r"\s", value):
        raise AuthError(f"{ENV_KEY} is empty or contains whitespace")
    return value


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    roots=None,
) -> Settings:
    """Resolve the API key and config location. Raises AuthError (exit 3) when
    there is no usable key. Registers the key for redaction."""
    env = os.environ if env is None else env
    cdir = config_dir(env)
    paid = _truthy(env.get(ENV_PAID_FLAG))

    key = env.get(ENV_KEY)
    if key:
        key = _clean_key(key)
        register_secret(key)
        return Settings(api_key=key, key_source="env", config_dir=cdir, paid_project=paid)

    path = cdir / KEY_FILE_NAME
    if not path.exists():
        raise AuthError(
            f"no API key: set {ENV_KEY} in the environment, or add a {ENV_KEY} line to {path} (mode 0600). "
            "Create the key in Google AI Studio on a billing-linked project."
        )
    resolved = path.resolve()
    use_roots = list(roots) if roots is not None else list(privacy.forbidden_roots())
    if privacy.is_inside(path.absolute(), use_roots) or privacy.is_inside(resolved, use_roots):
        raise AuthError("key file is inside the repository tree, refusing to read it")
    check_private_file(resolved, "key file")
    values = parse_env_text(resolved.read_text(encoding="utf-8"))
    key = values.get(ENV_KEY)
    if not key:
        raise AuthError(f"key file does not define {ENV_KEY}")
    key = _clean_key(key)
    register_secret(key)
    paid = paid or _truthy(values.get(ENV_PAID_FLAG))
    return Settings(api_key=key, key_source="file", config_dir=cdir, paid_project=paid)


def key_status(env: Optional[Mapping[str, str]] = None, roots=None) -> str:
    """Human-readable, secret-free description of where the key would come
    from. Used by `check --dry-run`."""
    env = os.environ if env is None else env
    if env.get(ENV_KEY):
        return f"{ENV_KEY} is set in the environment"
    path = config_dir(env) / KEY_FILE_NAME
    if path.exists():
        return f"key file present: {path}"
    return f"no key (set {ENV_KEY} or create {path} with mode 0600)"
