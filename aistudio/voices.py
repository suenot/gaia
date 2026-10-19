"""
Voices: the Voice Library listing, deletion, the local alias registry and the
verbatim consent statements Google requires for voice replication.

Local state lives in ${XDG_CONFIG_HOME:-~/.config}/gaia/ (never in the repo):
  voices.json          alias -> voice id / voicekey_... (mode 0600; a voicekey is
                       an encrypted voice profile, treat it like the API key)
  voice-consent.jsonl  append-only consent log (see voice_clone.py)
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import AuthError, UsageError
from . import config
from .client import GeminiClient, pick

# --------------------------------------------------------------------------- #
# Models and prebuilt voices (https://ai.google.dev/gemini-api/docs/speech-generation)
# --------------------------------------------------------------------------- #
MODEL_FLASH = "gemini-3.8-flash-tts"
MODEL_FLASH_LITE = "gemini-3.8-flash-lite-tts"
MODEL_TRANSCRIBE = "gemini-3.5-transcribe"
MODEL_ALIASES = {"flash": MODEL_FLASH, "flash-lite": MODEL_FLASH_LITE}

PREBUILT_VOICES: Tuple[str, ...] = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
    "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus",
    "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)

VOICE_TYPES = ("prebuilt", "prompted", "replicated")
STORED_TTL_NOTE = "1 year from last use"
KEY_TTL = timedelta(days=7)

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


# --------------------------------------------------------------------------- #
# Consent statements (voice replication guide, "Supported consent phrases by
# language"; the recording must recite the statement for its language verbatim)
# --------------------------------------------------------------------------- #
CHECKLIST = (
    "Only your own voice. Never record or submit anyone else's voice, even with their permission.",
    "Record the reference clip and the consent clip in one sitting: same speaker, same microphone, same room.",
    "Quiet room: no music, no echo, no overlapping voices.",
    "Reference clip: 10-30 s of clean, natural speech. Consent clip: the statement above, nothing else.",
    "24 kHz mono 16-bit WAV is recommended (gaia converts other formats into a temp dir).",
    "Keep both files outside the repository. They are biometric data.",
)


def _locale_index() -> Dict[str, str]:
    return {k.lower(): k for k in CONSENT_STATEMENTS}


def normalize_lang(lang: str) -> str:
    """'ru-ru' -> 'ru-RU'; a bare 'ru' works when exactly one locale matches."""
    index = _locale_index()
    key = (lang or "").strip().lower().replace("_", "-")
    if key in index:
        return index[key]
    primary = [loc for low, loc in index.items() if low.split("-")[0] == key]
    if len(primary) == 1:
        return primary[0]
    if primary:
        raise UsageError(f"--lang {lang!r} is ambiguous; pick one of: {', '.join(sorted(primary))}")
    raise UsageError(f"--lang {lang!r}: no consent statement for it. Run: consent-text --list")


def consent_statement(lang: str) -> str:
    return CONSENT_STATEMENTS[normalize_lang(lang)][1]


def consent_text_block(lang: str) -> str:
    loc = normalize_lang(lang)
    name, statement = CONSENT_STATEMENTS[loc]
    lines = [
        f"Consent statement for {name} ({loc}).",
        "Read it aloud exactly as written, in your own voice. Custom wording is rejected:",
        "",
        f"  {statement}",
        "",
        "Recording checklist:",
    ]
    lines += [f"  - {item}" for item in CHECKLIST]
    return "\n".join(lines)


def consent_locales() -> List[Tuple[str, str]]:
    return [(loc, name) for loc, (name, _) in sorted(CONSENT_STATEMENTS.items())]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = _FRACTION_RE.sub(r"\1", value.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Local alias registry
# --------------------------------------------------------------------------- #
@dataclass
class VoiceEntry:
    alias: str
    model: str
    language_code: str
    store: bool
    created: str
    expires: Optional[str] = None
    voice_id: Optional[str] = None  # voice_... when store=True
    voice_key: Optional[str] = field(default=None, repr=False)  # voicekey_... when store=False

    @property
    def value(self) -> str:
        """What goes into the synthesis request."""
        v = self.voice_id if self.store else self.voice_key
        if not v:
            raise UsageError(f"registry entry {self.alias!r} has no usable voice id/key")
        return v

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        exp = parse_time(self.expires)
        return bool(exp and exp <= (now or utcnow()))


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        config.check_private_file(self.path, "voice registry")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise AuthError("voice registry is not valid JSON; fix or remove it") from None
        return dict(data.get("voices", {}))

    def _write(self, voices: Dict[str, dict]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "voices": voices}, ensure_ascii=False, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=".voices-", dir=str(self.path.parent))
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def all(self) -> List[VoiceEntry]:
        return [VoiceEntry(**v) for _, v in sorted(self._read().items())]

    def get(self, alias: str) -> Optional[VoiceEntry]:
        raw = self._read().get(alias)
        return VoiceEntry(**raw) if raw else None

    def find_by_id(self, voice_id: str) -> Optional[VoiceEntry]:
        for entry in self.all():
            if entry.voice_id == voice_id:
                return entry
        return None

    def add(self, entry: VoiceEntry) -> None:
        if not ALIAS_RE.match(entry.alias):
            raise UsageError("--alias: use 1-32 chars from a-z 0-9 _ - (first one alphanumeric)")
        voices = self._read()
        if entry.alias in voices:
            raise UsageError(f"alias {entry.alias!r} already exists; pick another or delete it first")
        if entry.alias in PREBUILT_VOICES or entry.alias.capitalize() in PREBUILT_VOICES:
            raise UsageError(f"alias {entry.alias!r} collides with a prebuilt voice name")
        voices[entry.alias] = asdict(entry)
        self._write(voices)

    def remove(self, alias: str) -> bool:
        voices = self._read()
        if alias not in voices:
            return False
        del voices[alias]
        self._write(voices)
        return True

    def check_alias_free(self, alias: str) -> None:
        if not ALIAS_RE.match(alias):
            raise UsageError("--alias: use 1-32 chars from a-z 0-9 _ - (first one alphanumeric)")
        if alias in self._read():
            raise UsageError(f"alias {alias!r} already exists; pick another or delete it first")
        if alias.capitalize() in PREBUILT_VOICES:
            raise UsageError(f"alias {alias!r} collides with a prebuilt voice name")


# --------------------------------------------------------------------------- #
# Resolving --voice for synthesis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResolvedVoice:
    value: str  # goes into speechConfig voice
    kind: str  # prebuilt | library | stored | key | alias-stored | alias-key
    label: str  # log-safe description (never contains a voicekey)
    custom: bool  # replicated/designed voice (single speaker per request)


def resolve_voice(spec: str, registry: Optional[Registry] = None, now: Optional[datetime] = None) -> ResolvedVoice:
    """Accepts a prebuilt name (Kore), a registry alias, a raw voice_... id, a
    raw voicekey_... key, or any other string (treated as a Voice Library
    speaker name; the API validates it)."""
    spec = (spec or "").strip()
    if not spec:
        raise UsageError("--voice is empty")
    if registry is not None:
        entry = registry.get(spec)
        if entry:
            if entry.is_expired(now):
                raise UsageError(
                    f"voice {spec!r} expired on {entry.expires}; replicate it again (voices replicate)"
                )
            kind = "alias-stored" if entry.store else "alias-key"
            return ResolvedVoice(entry.value, kind, f"alias {spec}", True)
    if spec.startswith("voicekey_"):
        return ResolvedVoice(spec, "key", "voicekey_<masked>", True)
    if spec.startswith("voice_"):
        return ResolvedVoice(spec, "stored", spec, True)
    if spec in PREBUILT_VOICES:
        return ResolvedVoice(spec, "prebuilt", spec, False)
    return ResolvedVoice(spec, "library", f"{spec} (Voice Library name, unverified)", False)


# --------------------------------------------------------------------------- #
# Listing / deleting through the API
# --------------------------------------------------------------------------- #
def list_filters(
    lang: Optional[str] = None,
    type_: Optional[str] = None,
    gender: Optional[str] = None,
    pitch: Optional[str] = None,
    search: Optional[str] = None,
    page_size: int = 50,
) -> dict:
    if type_ and type_ not in VOICE_TYPES:
        raise UsageError(f"--type must be one of {', '.join(VOICE_TYPES)}")
    return {
        "language_code": [lang] if lang else None,
        "type_": [type_] if type_ else None,
        "gender": [gender] if gender else None,
        "pitch": [pitch] if pitch else None,
        "search": search,
        "page_size": page_size,
    }


def list_remote(client: GeminiClient, filters: dict, fetch_all: bool = False, max_pages: int = 20) -> List[dict]:
    voices: List[dict] = []
    token: Optional[str] = None
    for _ in range(max_pages):
        page = client.list_voices(**filters, page_token=token)
        voices.extend(page.get("voices", []) or [])
        token = pick(page, "next_page_token", "nextPageToken")
        if not token or not fetch_all:
            break
    return voices


def format_voice_row(v: dict) -> str:
    """One log-safe line: metadata only. A voicekey, if present, is not shown."""
    ident = pick(v, "id") or ("(key voice)" if pick(v, "key") else "?")
    cols = [
        str(ident),
        str(pick(v, "type", default="?")),
        str(pick(v, "language_code", "languageCode", default="-")),
        str(pick(v, "gender", default="-")),
        str(pick(v, "pitch", default="-")),
    ]
    extra = pick(v, "description", default="")
    exp = pick(v, "expire_time", "expireTime")
    line = " | ".join(cols)
    if exp:
        line += f" | expires {exp}"
    if extra:
        line += f" | {str(extra)[:70]}"
    return line


def plan_delete(registry: Registry, name: str) -> List[str]:
    """What `delete_voice` would do, without doing it (for --dry-run)."""
    entry = registry.get(name)
    if entry and entry.store:
        return [f"would delete {entry.voice_id} at Google", f"would remove alias {entry.alias!r} from the local registry"]
    if entry:
        return [f"would remove alias {entry.alias!r} from the local registry (stateless key: nothing is stored at Google)"]
    if name.startswith("voice_"):
        return [f"would delete {name} at Google"]
    raise UsageError(f"unknown voice {name!r}: give a registry alias or a voice_... id")


def delete_voice(
    client: Optional[GeminiClient], registry: Registry, name: str
) -> List[str]:
    """Delete by alias or raw voice id. Returns human-readable actions taken.
    Stored voices are deleted server-side; stateless keys only exist locally
    (and expire on their own after 7 days)."""
    actions: List[str] = []
    entry = registry.get(name)
    voice_id: Optional[str] = None
    if entry:
        if entry.store:
            voice_id = entry.voice_id
        else:
            actions.append(
                "stateless key: nothing stored at Google; it stops working after its 7 day TTL"
            )
    elif name.startswith("voice_"):
        voice_id = name
        entry = registry.find_by_id(name)
    else:
        raise UsageError(f"unknown voice {name!r}: give a registry alias or a voice_... id")
    if voice_id:
        if client is None:
            raise UsageError("internal: API client required to delete a stored voice")
        client.delete_voice(voice_id)
        actions.append(f"deleted {voice_id} at Google")
    if entry and registry.remove(entry.alias):
        actions.append(f"removed alias {entry.alias!r} from the local registry")
    return actions


# --------------------------------------------------------------------------- #
# The 30 locale statements, copied verbatim from
# https://ai.google.dev/gemini-api/docs/voice-replication (fetched 2026-10-19).
# tests/aistudio/fixtures/consent_statements.json is an independent copy that
# the tests compare against, byte for byte.
# --------------------------------------------------------------------------- #
CONSENT_STATEMENTS: Dict[str, Tuple[str, str]] = {
    "ar-XA": (
        "Arabic",
        "أنا مالك هذا الصوت وأوافق على أن تستخدم Google هذا الصوت لإنشاء نموذج صوتي اصطناعي.",
    ),
    "bn-IN": (
        "Bengali",
        "আমি এই ভয়েসের মালিক এবং আমি একটি সিন্থেটিক ভয়েস মডেল তৈরি করতে এই ভয়েস ব্যবহার করে Google-এর সাথে সম্মতি দিচ্ছি।",
    ),
    "zh-CN": (
        "Chinese (Simplified)",
        "我是此声音的拥有者并授权谷歌使用此声音创建语音合成模型",
    ),
    "nl-NL": (
        "Dutch",
        "Ik ben de eigenaar van deze stem en ik geef Google toestemming om deze stem te gebruiken om een synthetisch stemmodel te maken.",
    ),
    "en-US": (
        "English (US)",
        "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
    ),
    "en-GB": (
        "English (UK)",
        "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
    ),
    "en-IN": (
        "English (India)",
        "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
    ),
    "en-AU": (
        "English (Australia)",
        "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
    ),
    "fr-FR": (
        "French (France)",
        "Je suis le propriétaire de cette voix et j'autorise Google à utiliser cette voix pour créer un modèle de voix synthétique.",
    ),
    "fr-CA": (
        "French (Canada)",
        "Je suis le propriétaire de cette voix et j'autorise Google à utiliser cette voix pour créer un modèle de voix synthétique.",
    ),
    "de-DE": (
        "German",
        "Ich bin der Eigentümer dieser Stimme und bin damit einverstanden, dass Google diese Stimme zur Erstellung eines synthetischen Stimmmodells verwendet.",
    ),
    "gu-IN": (
        "Gujarati",
        "હું આ વોઈસનો માલિક છું અને સિન્થેટિક વોઈસ મોડલ બનાવવા માટે આ વોઈસનો ઉપયોગ કરીને google ને હું સંમતિ આપું છું",
    ),
    "hi-IN": (
        "Hindi",
        "मैं इस आवाज का मालिक हूं और मैं सिंथेटिक आवाज मॉडल बनाने के लिए Google को इस आवाज का उपयोग करने की सहमति देता हूं",
    ),
    "id-ID": (
        "Indonesian",
        "Saya pemilik suara ini dan saya menyetujui Google menggunakan suara ini untuk membuat model suara sintetis.",
    ),
    "it-IT": (
        "Italian",
        "Sono il proprietario di questa voce e acconsento che Google la utilizzi per creare un modello di voce sintetica.",
    ),
    "ja-JP": (
        "Japanese",
        "私はこの音声の所有者であり、Googleがこの音声を使用して音声合成モデルを作成することを承認します。",
    ),
    "kn-IN": (
        "Kannada",
        "ನಾನು ಈ ಧ್ವನಿಯ ಮಾಲಿಕ ಮತ್ತು ಸಂಶ್ಲೇಷಿತ ಧ್ವನಿ ಮಾದರಿಯನ್ನು ರಚಿಸಲು ಈ ಧ್ವನಿಯನ್ನು ಬಳಸಿಕೊಂಡುಗೂಗಲ್ ಗೆ ನಾನು ಸಮ್ಮತಿಸುತ್ತೇನೆ.",
    ),
    "ko-KR": (
        "Korean",
        "나는 이 음성의 소유자이며 구글이 이 음성을 사용하여 음성 합성 모델을 생성할 것을 허용합니다.",
    ),
    "ml-IN": (
        "Malayalam",
        "ഈ ശബ്ദത്തിന്റെ ഉടമ ഞാനാണ്, ഒരു സിന്തറ്റിക് വോയ്സ് മോഡൽ സൃഷ്ടിക്കാൻ ഈ ശബ്ദം ഉപയോഗിക്കുന്നതിന് ഞാൻ Google-ന് സമ്മതം നൽകുന്നു.",
    ),
    "mr-IN": (
        "Marathi",
        "मी या आवाजाचा मालक आहे आणि सिंथेटिक व्हॉइस मॉडेल तयार करण्यासाठी हा आवाज वापरण्यासाठी मी Google ला संमती देतो",
    ),
    "pl-PL": (
        "Polish",
        "Jestem właścicielem tego głosu i wyrażam zgodę na wykorzystanie go przez Google w celu utworzenia syntetycznego modelu głosu.",
    ),
    "pt-BR": (
        "Portuguese (Brazil)",
        "Eu sou o proprietário desta voz e autorizo o Google a usá-la para criar um modelo de voz sintética.",
    ),
    "ru-RU": (
        "Russian",
        "Я являюсь владельцем этого голоса и даю согласие Google на использование этого голоса для создания модели синтетического голоса.",
    ),
    "es-ES": (
        "Spanish (Spain)",
        "Soy el propietario de esta voz y doy mi consentimiento para que Google la utilice para crear un modelo de voz sintética.",
    ),
    "es-US": (
        "Spanish (US)",
        "Soy el propietario de esta voz y doy mi consentimiento para que Google la utilice para crear un modelo de voz sintética.",
    ),
    "ta-IN": (
        "Tamil",
        "நான் இந்த குரலின் உரிமையாளர் மற்றும் செயற்கை குரல் மாதிரியை உருவாக்க இந்த குரலை பயன்படுத்த குகல்க்கு நான் ஒப்புக்கொள்கிறேன்.",
    ),
    "te-IN": (
        "Telugu",
        "నేను ఈ వాయిస్ యజమానిని మరియు సింతటిక్ వాయిస్ మోడల్ ని రూపొందించడానికి ఈ వాయిస్ ని ఉపయోగించడానికి googleకి నేను సమ్మతిస్తున్నాను.",
    ),
    "th-TH": (
        "Thai",
        "ฉันเป็นเจ้าของเสียงนี้ และฉันยินยอมให้ Google ใช้เสียงนี้เพื่อสร้างแบบจำลองเสียงสังเคราะห์",
    ),
    "tr-TR": (
        "Turkish",
        "Bu sesin sahibi benim ve Google'ın bu sesi kullanarak sentetik bir ses modeli oluşturmasına izin veriyorum.",
    ),
    "vi-VN": (
        "Vietnamese",
        "Tôi là chủ sở hữu giọng nói này và tôi đồng ý cho Google sử dụng giọng nói này để tạo mô hình giọng nói tổng hợp.",
    ),
}
