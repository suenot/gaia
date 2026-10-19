"""
AI Studio speech support for GAIA: speech-to-text, text-to-speech and voice
replication through the documented Gemini API (an AI Studio key, the
`google-genai` SDK). Unlike the rest of GAIA this module does NOT use the
logged-in Camoufox session and never touches the AI Studio web UI.

Layout (see docs/aistudio-speech-research.md, section 7):

    config.py       key + path resolution, secret redaction
    privacy.py      private-audio path guards, output-dir policy, 0600 writes
    client.py       the only module that talks to google-genai (one place to
                    switch generateContent <-> Interactions, error mapping)
    audio.py        ffprobe/ffmpeg preflight, WAV helpers, text chunking
    stt.py          transcription request builders + parsers + writers
    tts.py          speech request builders + parsers + synthesis driver
    voices.py       voice listing/deletion, local registry, consent texts
    voice_clone.py  voice replication guard rails and the consent log

CLI entry point: `aistudio_speech.py` in the repository root.

Exit codes (shared by every subcommand):
    0 ok   2 bad args   3 no key / auth failed   4 audio or privacy preflight
    failed   5 API refused (region, quota, consent mismatch, server error)
    6 output not produced
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_AUDIO = 4
EXIT_API = 5
EXIT_OUTPUT = 6


class AIStudioError(Exception):
    """Base class. `exit_code` is what the CLI exits with. Messages must never
    contain secrets or transcript text; the CLI redacts them once more anyway."""

    exit_code = 1


class UsageError(AIStudioError):
    exit_code = EXIT_USAGE


class AuthError(AIStudioError):
    """Missing/invalid key, or a key file with unsafe permissions."""

    exit_code = EXIT_AUTH


class AudioError(AIStudioError):
    """Audio preflight failed, or an input/privacy rule refused the path."""

    exit_code = EXIT_AUDIO


class ApiError(AIStudioError):
    """The API refused or failed (region, quota, consent mismatch, 5xx...)."""

    exit_code = EXIT_API


class OutputError(AIStudioError):
    exit_code = EXIT_OUTPUT
