"""
The one module that talks to the `google-genai` SDK.

Everything else builds plain REST-shaped JSON (as documented on ai.google.dev)
and hands it to `GeminiClient`; responses come back as plain dicts. That keeps
the callers independent of SDK classes and gives a single switch between the
two request surfaces the Gemini API offers for speech:

    generate-content  models/{model}:generateContent   (default; the classic API)
    interactions      /v1beta/interactions             (GA since June 2026 per
                                                        Google, SDK `interactions`)

Voices (`/v1beta/voices`) and the Files API go through the SDK for both.

Rules enforced here:
  * the API key is passed explicitly and Vertex mode is forced off (the SDK
    would otherwise read GOOGLE_API_KEY / GOOGLE_GENAI_USE_VERTEXAI);
  * SDK-level retries are neutralized (the Voices client of google-genai always
    retries at least once); idempotent calls get a small retry of our own, and
    `voices.create` (not idempotent) goes out as ONE plain REST POST via httpx
    (documented: POST /v1beta/voices) so it can never be sent twice;
  * every SDK exception is mapped to an AIStudioError with a redacted, capped
    message and re-raised `from None` so no key can leak through a traceback;
  * Interactions requests always carry `store=false`, otherwise Google keeps
    the prompts and audio for up to 55 days.
"""

from __future__ import annotations

import base64
import copy
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from . import ApiError, AIStudioError, AuthError
from .config import Settings, redact

API_GENERATE_CONTENT = "generate-content"
API_INTERACTIONS = "interactions"
APIS = (API_GENERATE_CONTENT, API_INTERACTIONS)

API_ROOT = "https://generativelanguage.googleapis.com"
MAX_ERROR_CHARS = 300
_TRANSIENT_STATUS = {500, 502, 503, 504}
_RETRY_DELAYS = (2.0, 6.0)


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
class HttpStatusError(Exception):
    """A failed raw REST call, shaped like the SDK errors map_error understands."""

    def __init__(self, code: int, status: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _status_of(exc: BaseException) -> Optional[int]:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_connection_error(exc: BaseException) -> bool:
    names = {c.__name__ for c in type(exc).__mro__}
    return bool(names & {"TransportError", "APIConnectionError", "APITimeoutError", "ConnectError", "TimeoutException"})


def _clip(text: str) -> str:
    text = " ".join(redact(text).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "..."


def map_error(exc: BaseException, what: str = "request") -> AIStudioError:
    """Translate an SDK/transport exception into an AIStudioError (exit code
    3 for auth, 5 for everything the API or network refused)."""
    if isinstance(exc, AIStudioError):
        return exc
    status = _status_of(exc)
    status_str = str(getattr(exc, "status", "") or "")
    raw = str(getattr(exc, "message", "") or exc)
    low = raw.lower()

    if _is_connection_error(exc):
        return ApiError(f"{what}: network error, no response from the API")
    if "api key not valid" in low or "api_key_invalid" in low or "api key expired" in low or status == 401 \
            or status_str == "UNAUTHENTICATED":
        return AuthError(f"{what}: the API key was rejected. Create a new key in Google AI Studio.")
    if "location is not supported" in low or "not available in your region" in low or "not supported in your" in low:
        return ApiError(
            f"{what}: refused for the account's/IP's region (FAILED_PRECONDITION). Voice replication is "
            "not available everywhere; see the README, section Region limits."
        )
    if status == 429 or status_str == "RESOURCE_EXHAUSTED":
        return ApiError(f"{what}: quota or rate limit exhausted (RESOURCE_EXHAUSTED): {_clip(raw)}")
    if status == 403 or status_str == "PERMISSION_DENIED":
        return ApiError(f"{what}: permission denied (key, billing or region): {_clip(raw)}")
    if status == 404 or status_str == "NOT_FOUND":
        return ApiError(f"{what}: not found (check the model name / voice id): {_clip(raw)}")
    if status is not None and status >= 500:
        return ApiError(f"{what}: Google server error {status}: {_clip(raw)}")
    if status is not None:
        return ApiError(f"{what}: refused ({status} {status_str or 'error'}): {_clip(raw)}")
    return ApiError(f"{what}: unexpected {type(exc).__name__}: {_clip(raw)}")


def is_transient(exc: BaseException) -> bool:
    status = _status_of(exc)
    return _is_connection_error(exc) or (status in _TRANSIENT_STATUS)


# --------------------------------------------------------------------------- #
# Body helpers
# --------------------------------------------------------------------------- #
def _dump(obj: Any) -> dict:
    """SDK model -> plain JSON-able dict (camelCase for generateContent
    responses, snake_case for voices/interactions, as the REST API emits)."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return obj.model_dump(mode="json", by_alias=True, exclude_none=True)


def describe_body(body: Any) -> Any:
    """Copy of a request body that is safe to print: long strings (text,
    base64 audio) are replaced by their length, voice keys are masked."""
    def walk(node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        if isinstance(node, str):
            if key == "data" and not node.startswith("<"):
                return f"<base64: {len(node)} chars, ~{len(node) * 3 // 4} bytes>"
            if key == "text":
                return f"<text: {len(node)} chars>"
            if node.startswith("voicekey_"):
                return "voicekey_<masked>"
            if len(node) > 120:
                return f"<string: {len(node)} chars>"
        return node
    return walk(copy.deepcopy(body))


@dataclass(frozen=True)
class Request:
    """A fully built API request in the documented REST shape. `body` is what
    Google documents for `api` (camelCase for generateContent, snake_case for
    Interactions); `model` travels separately because generateContent carries
    it in the URL path."""

    api: str
    model: str
    body: dict

    def golden(self) -> dict:
        return {"api": self.api, "model": self.model, "body": self.body}

    def printable(self) -> dict:
        return {"api": self.api, "model": self.model, "body": describe_body(self.body)}


@dataclass(frozen=True)
class UploadedFile:
    name: str  # "files/abc123"
    uri: str
    mime_type: str


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GeminiClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        api: str = API_GENERATE_CONTENT,
        sdk: Any = None,
        timeout_s: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
        http: Any = None,
        api_key: Optional[str] = None,
    ) -> None:
        if api not in APIS:
            raise ValueError(f"api must be one of {APIS}")
        self.api = api
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._settings = settings
        self._sdk = sdk
        self._http = http  # httpx.Client for the one raw REST call (voices.create)
        self._api_key = settings.api_key if settings is not None else api_key
        # httpx logs request lines at INFO; keep the SDK quiet regardless.
        for noisy in ("httpx", "httpcore", "google_genai", "google.genai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    # -- construction ------------------------------------------------------ #
    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            if self._settings is None:
                raise AuthError("no API key available")
            try:
                from google import genai
                from google.genai import types
            except ImportError:
                raise AIStudioError(
                    "google-genai is not installed. Run: pip install -r requirements-aistudio.txt"
                ) from None
            self._sdk = genai.Client(
                api_key=self._settings.api_key,
                vertexai=False,
                http_options=types.HttpOptions(
                    timeout=int(self._timeout_s * 1000),
                    # We retry ourselves. The status override makes the SDK's own
                    # retry (min. one on its Voices/Interactions client) a no-op.
                    retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[599]),
                ),
            )
        return self._sdk

    @staticmethod
    def _types() -> Any:
        from google.genai import types

        return types

    # -- plumbing ---------------------------------------------------------- #
    def _call(self, fn: Callable[[], Any], what: str, retry: bool = True) -> Any:
        attempts = len(_RETRY_DELAYS) + 1 if retry else 1
        for attempt in range(attempts):
            try:
                return fn()
            except AIStudioError:
                raise
            except Exception as exc:  # noqa: BLE001 - mapped below, never re-raised raw
                if retry and attempt < attempts - 1 and is_transient(exc):
                    self._sleep(_RETRY_DELAYS[attempt])
                    continue
                raise map_error(exc, what) from None
        raise ApiError(f"{what}: retries exhausted")  # pragma: no cover

    # -- no-cost reachability ---------------------------------------------- #
    def list_model_names(self, limit: int = 1000) -> List[str]:
        def go() -> List[str]:
            names: List[str] = []
            for model in self.sdk.models.list():
                names.append(str(getattr(model, "name", "") or ""))
                if len(names) >= limit:
                    break
            return names

        return self._call(go, "models.list")

    # -- voices ------------------------------------------------------------- #
    def list_voices(self, **filters: Any) -> dict:
        """filters use the SDK names: language_code, type_, gender, pitch,
        persona, contexts, accent, region_code, search, page_size, page_token
        (list filters are lists of strings)."""
        clean = {k: v for k, v in filters.items() if v not in (None, [], "")}
        return _dump(self._call(lambda: self.sdk.voices.list(**clean), "voices.list"))

    def get_voice(self, voice_id: str) -> dict:
        return _dump(self._call(lambda: self.sdk.voices.get(id=voice_id), "voices.get"))

    def delete_voice(self, voice_id: str) -> None:
        self._call(lambda: self.sdk.voices.delete(id=voice_id), "voices.delete")

    def create_voice(self, request: dict) -> dict:
        """`request` = {"store": bool, "voice": {...}} exactly as documented.
        Never retried: a retry after a server-side success would create a
        second voice."""
        if "store" not in request or not isinstance(request["store"], bool):
            raise ValueError("create_voice: 'store' must be passed explicitly as a bool")
        return self._post_json("/v1beta/voices", request, "voices.create")

    def _post_json(self, path: str, body: dict, what: str) -> dict:
        """One plain REST POST, never retried. Same header the SDK sends."""
        if not self._api_key:
            raise AuthError("no API key available")
        try:
            import httpx
        except ImportError:
            raise AIStudioError("httpx is missing (installed with google-genai)") from None
        http = self._http or httpx.Client()
        try:
            resp = http.post(
                API_ROOT + path,
                json=body,
                headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            raise map_error(exc, what) from None
        finally:
            if self._http is None:
                http.close()
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise map_error(
                HttpStatusError(resp.status_code, str(err.get("status", "")), str(err.get("message", resp.reason_phrase))),
                what,
            ) from None
        try:
            return resp.json()
        except ValueError:
            raise ApiError(f"{what}: the API returned a non-JSON answer") from None

    # -- files -------------------------------------------------------------- #
    def upload_file(self, path: str, mime_type: str, display_name: str) -> UploadedFile:
        types = self._types()

        def go() -> UploadedFile:
            cfg = types.UploadFileConfig(mime_type=mime_type, display_name=display_name)
            # Pass an open handle, not the path: with a path the SDK sends the local
            # file name to Google in the `x-goog-upload-file-name` header.
            with open(path, "rb") as handle:
                f = self.sdk.files.upload(file=handle, config=cfg)
            for _ in range(60):  # wait until ACTIVE (audio normally is at once)
                state = str(getattr(f, "state", "") or "")
                if "PROCESSING" not in state.upper():
                    break
                self._sleep(1.0)
                f = self.sdk.files.get(name=f.name)
            if "FAILED" in str(getattr(f, "state", "")).upper():
                raise ApiError("files.upload: the file failed server-side processing")
            return UploadedFile(name=f.name, uri=f.uri, mime_type=f.mime_type or mime_type)

        return self._call(go, "files.upload")

    def delete_file(self, name: str) -> bool:
        """Best effort; returns False (and never raises) when deletion fails so
        callers can warn."""
        try:
            self._call(lambda: self.sdk.files.delete(name=name), "files.delete")
            return True
        except AIStudioError:
            return False

    # -- generation --------------------------------------------------------- #
    def generate(self, model: str, body: dict, what: str) -> dict:
        """Send one speech/transcription request. `body` is in the shape
        documented for `self.api`; returns the response as a plain dict."""
        if self.api == API_INTERACTIONS:
            return self._interactions(model, body, what)
        return self._generate_content(model, body, what)

    def _generate_content(self, model: str, body: dict, what: str) -> dict:
        types = self._types()
        contents = copy.deepcopy(body["contents"])
        for content in contents:
            for part in content.get("parts", []):
                blob = part.get("inlineData")
                if blob and isinstance(blob.get("data"), str):
                    blob["data"] = base64.b64decode(blob["data"])  # SDK re-encodes on the wire
        cfg = dict(body.get("generationConfig") or {})
        cfg["automaticFunctionCalling"] = {"disable": True}
        contents_t = [types.Content.model_validate(c) for c in contents]
        config_t = types.GenerateContentConfig.model_validate(cfg)
        return _dump(
            self._call(
                lambda: self.sdk.models.generate_content(model=model, contents=contents_t, config=config_t),
                what,
            )
        )

    def _interactions(self, model: str, body: dict, what: str) -> dict:
        kwargs = {k: v for k, v in body.items() if k != "model"}
        kwargs["store"] = False  # never let Google keep the interaction
        return _dump(
            self._call(lambda: self.sdk.interactions.create(model=model, **kwargs), what)
        )


# --------------------------------------------------------------------------- #
# Small shared parsing helpers (REST responses use camelCase or snake_case
# depending on the surface; accept both).
# --------------------------------------------------------------------------- #
def pick(node: Optional[dict], *names: str, default: Any = None) -> Any:
    if not isinstance(node, dict):
        return default
    for name in names:
        if name in node and node[name] is not None:
            return node[name]
    return default


def decode_b64(text: str) -> bytes:
    """Decode base64 in either alphabet. Google sends the standard one; the SDK
    re-serializes bytes it parsed as URL-safe. `base64.b64decode` would silently
    DROP the '-' and '_' characters of the URL-safe form and corrupt audio."""
    text = text.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4))


_DURATION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*s?\s*$")


def parse_offset(value: Any) -> Optional[float]:
    """'3.5s' / '3.5' / 3.5 -> 3.5 seconds, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION_RE.match(str(value))
    return float(m.group(1)) if m else None
