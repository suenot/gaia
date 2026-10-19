#!/usr/bin/env python3
"""
Google AI Studio speech from the command line: speech-to-text, text-to-speech
and voice replication through the documented Gemini API (an AI Studio key and
the `google-genai` SDK).

This is the one GAIA tool that does NOT use the logged-in Camoufox session: it
calls a documented API with a key YOU create. See README, section "AI Studio
speech", and docs/aistudio-speech-research.md.

Usage
-----
    python3 aistudio_speech.py check
    python3 aistudio_speech.py voices list --lang ru-RU --type prebuilt
    python3 aistudio_speech.py consent-text --lang ru-RU
    python3 aistudio_speech.py voices replicate --reference /abs/ref.wav \\
        --consent /abs/consent.wav --lang ru-RU --alias me --no-store --i-am-the-speaker
    python3 aistudio_speech.py tts --text-file script.txt --voice me --out-dir /abs/out
    python3 aistudio_speech.py stt --input /abs/call.m4a --lang ru-RU --diarize --words \\
        --format txt --format srt --out-dir /abs/out

Exit codes: 0 ok | 2 bad args | 3 no key / auth failed | 4 audio or privacy
preflight failed | 5 API refused (region, quota, consent mismatch, server error)
| 6 output not produced.

Privacy: key from GEMINI_API_KEY or ~/.config/gaia/aistudio.env (mode 0600);
private audio only from absolute paths outside this repo; outputs only to a
directory you choose, outside this repo; nothing personal is ever logged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional

from aistudio import EXIT_OK, AIStudioError, OutputError, UsageError
from aistudio import audio, config, privacy, stt, tts, voice_clone, voices
from aistudio.client import API_GENERATE_CONTENT, APIS, GeminiClient, describe_body

ENV_API = "GAIA_AISTUDIO_API"
SUPPORTED_PYTHON = ((3, 11), (3, 13))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] warning: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def make_client(args: argparse.Namespace) -> "tuple[GeminiClient, config.Settings]":
    settings = config.load_settings()
    client = GeminiClient(settings, api=args.api, timeout_s=args.timeout)
    return client, settings


def open_registry() -> voices.Registry:
    return voices.Registry(config.registry_path())


def require_paid_ack(args: argparse.Namespace, settings: Optional[config.Settings], what: str) -> None:
    """Replication and STT send biometric/private audio. The free tier lets
    Google use content to improve its products, and no API call reveals the
    tier, so the caller must acknowledge it."""
    if getattr(args, "accept_unpaid_terms", False):
        warn(f"{what}: proceeding with --accept-unpaid-terms; on the free tier Google may use this content")
        return
    if settings is not None and settings.paid_project:
        return
    raise UsageError(
        f"{what} sends private audio to Google. Use a key from a billing-linked project (Paid Services: "
        f"content is not used to improve products) and set {config.ENV_PAID_FLAG}=1 (env or aistudio.env), "
        "or pass --accept-unpaid-terms to accept the free-tier terms for this run."
    )


def tty_confirm(phrase: str) -> bool:
    if not sys.stdin.isatty():
        return True  # non-interactive: the explicit flag is the confirmation
    answer = input(f'Type "{phrase}" to confirm you are the speaker in both recordings: ')
    return answer.strip().lower() == phrase.lower()


def print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def read_text_arg(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.text_file == "-":
        return sys.stdin.read()
    try:
        return Path(args.text_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"--text-file: cannot read ({exc.strerror})") from None


def check_lang_codes(codes: List[str]) -> List[str]:
    for c in codes:
        if not re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$", c):
            raise UsageError(f"--lang {c!r} is not a BCP-47 code such as ru-RU or en-US")
    return codes


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def cmd_check(args: argparse.Namespace) -> int:
    py = sys.version_info[:2]
    ok_py = SUPPORTED_PYTHON[0] <= py <= SUPPORTED_PYTHON[1]
    log(f"python {py[0]}.{py[1]} {'ok' if ok_py else '(GAIA supports 3.11-3.13)'}")
    try:
        import google.genai as genai

        log(f"google-genai {getattr(genai, '__version__', '?')}")
    except ImportError:
        warn("google-genai is not installed: pip install -r requirements-aistudio.txt")
    for tool in ("ffmpeg", "ffprobe"):
        log(f"{tool}: {'found' if shutil.which(tool) else 'MISSING (brew install ffmpeg)'}")
    log(f"api surface: {args.api}")
    log(f"config dir: {config.config_dir()}")
    log(config.key_status())

    if args.dry_run:
        log("dry run: no network call made")
        return EXIT_OK

    client, settings = make_client(args)
    log(f"key loaded from {settings.key_source} (value hidden); "
        f"paid project acknowledged: {'yes' if settings.paid_project else 'no'}")
    if not settings.paid_project:
        warn(f"cannot confirm a billing-linked project; set {config.ENV_PAID_FLAG}=1 once you have linked billing")
    names = client.list_model_names()
    log(f"models.list ok ({len(names)} models)")
    for model in (voices.MODEL_FLASH, voices.MODEL_FLASH_LITE, voices.MODEL_TRANSCRIBE):
        present = any(n.endswith("/" + model) or n == model for n in names)
        log(f"  {model}: {'listed' if present else 'not listed for this key'}")
    page = client.list_voices(page_size=1)
    log(f"voices.list ok ({len(page.get('voices', []) or [])} voice(s) in the first page)")
    log("note: whether voice replication is allowed for your region is only known when you try it")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# consent-text
# --------------------------------------------------------------------------- #
def cmd_consent_text(args: argparse.Namespace) -> int:
    if args.list or not args.lang:
        for loc, name in voices.consent_locales():
            print(f"{loc}\t{name}")
        return EXIT_OK
    if args.json:
        loc = voices.normalize_lang(args.lang)
        print_json({"language": loc, "statement": voices.consent_statement(loc)})
    else:
        print(voices.consent_text_block(args.lang))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# voices
# --------------------------------------------------------------------------- #
def cmd_voices_list(args: argparse.Namespace) -> int:
    if args.local:
        entries = open_registry().all()
        if not entries:
            log("no local aliases")
        for e in entries:
            exp = f" | expires {e.expires}" if e.expires else ""
            kind = "stored" if e.store else "stateless key"
            log(f"{e.alias} | {kind} | {e.language_code} | {e.model}{exp}" + (" | EXPIRED" if e.is_expired() else ""))
        return EXIT_OK
    client, _ = make_client(args)
    filters = voices.list_filters(args.lang, args.type, args.gender, args.pitch, args.search, args.page_size)
    rows = voices.list_remote(client, filters, fetch_all=args.all)
    for row in rows:
        print(voices.format_voice_row(row))
    log(f"{len(rows)} voice(s)" + ("" if args.all else " (first page; use --all for the rest)"))
    return EXIT_OK


def cmd_voices_replicate(args: argparse.Namespace) -> int:
    voice_clone.require_speaker_flag(args.i_am_the_speaker)  # before anything else, even before the key
    registry = open_registry()
    client = None
    settings = None
    if not args.dry_run:
        client, settings = make_client(args)
        require_paid_ack(args, settings, "voices replicate")
    result = voice_clone.replicate(
        client,
        registry,
        config.consent_log_path(),
        reference=args.reference,
        consent=args.consent,
        lang=args.lang,
        alias=args.alias,
        store=args.store,
        model=args.model,
        i_am_the_speaker=args.i_am_the_speaker,
        confirm=None if args.dry_run else tty_confirm,
        dry_run=args.dry_run,
        progress=log,
    )
    if args.dry_run:
        log("dry run: the request below would be sent (audio replaced by placeholders); nothing was sent or saved")
        print_json({"POST": "/v1beta/voices", "body": describe_body(result.request)})
        return EXIT_OK
    entry = result.entry
    assert entry is not None
    log(f"use it:  tts --voice {entry.alias} --text-file ... --out-dir /abs/out")
    log(f"delete:  voices delete {entry.alias}")
    if args.audition_out_dir:
        out_dir = privacy.output_dir(args.audition_out_dir, "--audition-out-dir")
        privacy.ensure_private_dir(out_dir)
        sentence = voice_clone.audition_sentence(entry.language_code)
        voice = voices.resolve_voice(entry.alias, registry)
        res = tts.synthesize(client, sentence, voice, model=entry.model, progress=log)
        path = privacy.write_private(out_dir / f"audition-{entry.alias}.wav", res.wav, overwrite=True)
        log(f"audition clip written ({res.duration_s:.1f} s): {path}. Listen to it; if it is not right: voices delete {entry.alias}")
    else:
        log("tip: add --audition-out-dir /abs/dir to synthesize a short check clip right away")
    return EXIT_OK


def cmd_voices_delete(args: argparse.Namespace) -> int:
    registry = open_registry()
    if args.dry_run:
        for action in voices.plan_delete(registry, args.name):
            log(action)
        log("dry run: nothing was deleted")
        return EXIT_OK
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("voices delete needs --yes when not run interactively")
        if input(f"Delete voice {args.name!r}? [y/N] ").strip().lower() not in {"y", "yes"}:
            log("cancelled")
            return EXIT_OK
    entry = registry.get(args.name)
    needs_api = (entry.store if entry else args.name.startswith("voice_"))
    client = make_client(args)[0] if needs_api else None
    for action in voices.delete_voice(client, registry, args.name):
        log(action)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# tts
# --------------------------------------------------------------------------- #
def cmd_tts(args: argparse.Namespace) -> int:
    text = read_text_arg(args)
    if not text.strip():
        raise UsageError("the text is empty")
    out_dir = privacy.output_dir(args.out_dir)
    model = voices.resolve_model(args.model)
    registry = open_registry()
    speakers = tts.parse_speaker_args(args.speaker or [])
    if bool(speakers) == bool(args.voice):
        raise UsageError("give exactly one of --voice (single speaker) or --speaker NAME=VOICE (dialogue)")
    if args.lang:
        check_lang_codes([args.lang])
    if args.name:
        privacy.check_slug(args.name)

    if speakers:
        turns = tts.parse_turns(text, list(speakers))
        resolved = tts.resolve_speakers(speakers, registry)
        chunks = [c for t in turns for c in audio.chunk_text(t.text, args.max_chars)]
        voice_label = ", ".join(f"{n}={v.label}" for n, v in resolved.items())
        first_voice = resolved[turns[0].speaker].value
        first_text = chunks[0]
    else:
        voice = voices.resolve_voice(args.voice, registry)
        chunks = audio.chunk_text(text, args.max_chars)
        voice_label = voice.label
        first_voice = voice.value
        first_text = chunks[0]

    log(f"tts: model {model}, voice {voice_label}, api {args.api}; {privacy.describe_text(text)}; {len(chunks)} request(s)")
    log(tts.describe_estimate(len(text), model))

    if args.dry_run:
        req = tts.build_speech_request(model, first_text, first_voice, args.style, args.lang, args.api)
        log("dry run: first request below (text elided); nothing was sent")
        print_json(req.printable())
        return EXIT_OK

    client, _ = make_client(args)
    if speakers:
        result = tts.synthesize_dialogue(
            client, turns, resolved, model=model, style=args.style, lang=args.lang,
            max_chars=args.max_chars, gap_ms=args.gap_ms, progress=log,
        )
    else:
        result = tts.synthesize(
            client, text, voice, model=model, style=args.style, lang=args.lang,
            max_chars=args.max_chars, gap_ms=args.gap_ms, progress=log,
        )

    privacy.ensure_private_dir(out_dir)
    base = args.name or privacy.digest_name("tts", text.encode("utf-8"))
    path = write_tts_output(result.wav, out_dir, base, args.format, args.overwrite)
    notes = []
    if result.stitched:
        notes.append("stitched locally")
    if args.format != "wav":
        notes.append(f"converted to {args.format}")
    log(f"wrote {path} ({result.duration_s:.1f} s, {result.requests} request(s))")
    if notes:
        warn(
            f"{' and '.join(notes)}: the file is no longer byte-identical to Google's output, so embedded "
            "C2PA provenance metadata may not survive. Do not claim it as human speech; label synthetic "
            "audio per each platform's rules."
        )
    return EXIT_OK


def write_tts_output(wav: bytes, out_dir: Path, base: str, fmt: str, overwrite: bool) -> Path:
    if fmt == "wav":
        return privacy.write_private(out_dir / f"{base}.wav", wav, overwrite)
    if fmt == "l16":
        return privacy.write_private(out_dir / f"{base}.l16", audio.parse_wav(wav).data, overwrite)
    final = out_dir / f"{base}.{fmt}"
    if final.exists() and not overwrite:
        raise UsageError(f"output file already exists (use --overwrite): {final.name}")
    tmp = audio.make_temp_dir()
    try:
        src = tmp / "in.wav"
        src.write_bytes(wav)
        encoded = audio.encode_output(src, tmp / f"out.{fmt}", fmt)
        return privacy.write_private(final, encoded.read_bytes(), overwrite)
    finally:
        audio.remove_temp_dir(tmp)


# --------------------------------------------------------------------------- #
# stt
# --------------------------------------------------------------------------- #
def load_vocabulary(args: argparse.Namespace) -> List[str]:
    terms = list(args.term or [])
    if args.vocab_file:
        try:
            lines = Path(args.vocab_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise UsageError(f"--vocab-file: cannot read ({exc.strerror})") from None
        terms += [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    return terms


def cmd_stt(args: argparse.Namespace) -> int:
    in_path = privacy.private_input(args.input, "--input")
    out_dir = privacy.output_dir(args.out_dir)
    if args.name:
        privacy.check_slug(args.name)
    formats = args.format or ["txt"]
    opts = stt.SttOptions(
        language_codes=check_lang_codes(args.lang or []),
        diarize=args.diarize,
        words=args.words,
        smart=args.smart,
        vocabulary=load_vocabulary(args),
    )
    stt.validate_options(opts)
    if any(f in ("srt", "vtt") for f in formats) and not opts.words:
        raise UsageError("--format srt/vtt needs word timestamps: add --words")
    model = args.model

    tmp = audio.make_temp_dir()
    try:
        prepared = stt.prepare_audio(in_path, opts, tmp)
        info = prepared.info
        size = prepared.path.stat().st_size
        transport = "inline" if size <= stt.INLINE_MAX_BYTES else "Files API (deleted afterwards)"
        log(
            f"stt: model {model}, api {args.api}; audio {info.duration_s:.1f} s, {size / 1e6:.1f} MB"
            f"{' (converted to FLAC 16 kHz mono)' if prepared.converted else ''}; transport {transport}"
        )
        log(f"estimate: ~${info.duration_s / 60 * 0.005:.3f} at the paid list price (~$0.005/min)")

        if args.dry_run:
            part = stt.AudioPart(prepared.mime_type, inline_bytes=size) if size <= stt.INLINE_MAX_BYTES \
                else stt.AudioPart(prepared.mime_type, uri="<files api uri>")
            req = stt.build_transcribe_request(part, opts, model, args.api)
            log("dry run: request below (audio elided); nothing was sent")
            print_json(req.printable())
            return EXIT_OK

        client, settings = make_client(args)
        require_paid_ack(args, settings, "stt")
        transcript, meta = stt.transcribe_file(client, prepared, opts, model, progress=log)
    finally:
        audio.remove_temp_dir(tmp)

    privacy.ensure_private_dir(out_dir)
    base = args.name or f"stt-{privacy.sha256_file(in_path)[:12]}"
    written = []
    for fmt in dict.fromkeys(formats):
        body = stt.render(fmt, transcript, meta)
        written.append(privacy.write_private(out_dir / f"{base}.{fmt}", body.encode("utf-8"), args.overwrite))
    if not written:
        raise OutputError("no output was produced")
    log(
        f"transcript: {len(transcript.text)} chars, {len(transcript.segments)} segment(s), "
        f"{len(transcript.words)} word timestamp(s), {len(transcript.speakers)} speaker(s)"
    )
    for p in written:
        log(f"wrote {p}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    # Global options are accepted before AND after the subcommand. The copy on
    # the subparsers uses SUPPRESS so it never overrides a value given earlier.
    def add_common(parser: argparse.ArgumentParser, suppress: bool) -> None:
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--api", choices=APIS, default=d(os.environ.get(ENV_API, API_GENERATE_CONTENT)),
                            help="request surface: generate-content (default) or interactions "
                                 f"(env {ENV_API}); Interactions requests are always sent with store=false")
        parser.add_argument("--timeout", type=float, default=d(300.0),
                            help="per-request timeout in seconds (default 300)")
        parser.add_argument("--verbose", action="store_true", default=d(False),
                            help="on unexpected errors print the redacted traceback")

    common = argparse.ArgumentParser(add_help=False)
    add_common(common, suppress=True)
    p = argparse.ArgumentParser(
        prog="aistudio_speech.py",
        description="Google AI Studio speech: STT, TTS and voice replication via the Gemini API.",
    )
    add_common(p, suppress=False)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", parents=[common], help="verify setup: key, SDK, ffmpeg, reachable models and voices (no cost)")
    c.add_argument("--dry-run", action="store_true", help="local checks only, no network")
    c.set_defaults(func=cmd_check)

    ct = sub.add_parser("consent-text", parents=[common], help="print the exact consent statement Google requires for a language")
    ct.add_argument("--lang", help="locale, e.g. ru-RU")
    ct.add_argument("--list", action="store_true", help="list the 30 supported locales")
    ct.add_argument("--json", action="store_true")
    ct.set_defaults(func=cmd_consent_text)

    v = sub.add_parser("voices", parents=[common], help="list, replicate and delete voices")
    vs = v.add_subparsers(dest="voices_command", required=True)

    vl = vs.add_parser("list", parents=[common], help="list the Voice Library (and your stored voices) or local aliases")
    vl.add_argument("--lang", help="BCP-47 filter, e.g. ru-RU")
    vl.add_argument("--type", choices=voices.VOICE_TYPES)
    vl.add_argument("--gender", choices=["female", "male", "neutral"])
    vl.add_argument("--pitch", choices=["low", "medium", "high"])
    vl.add_argument("--search", help="substring of name/description")
    vl.add_argument("--page-size", type=int, default=50)
    vl.add_argument("--all", action="store_true", help="follow pagination")
    vl.add_argument("--local", action="store_true", help="show the local alias registry instead")
    vl.set_defaults(func=cmd_voices_list)

    vr = vs.add_parser("replicate", parents=[common], help="create a voice from YOUR OWN reference + consent recordings")
    vr.add_argument("--reference", required=True, help="absolute path, 10-30 s of your clean speech")
    vr.add_argument("--consent", required=True, help="absolute path, you reading the consent statement")
    vr.add_argument("--lang", required=True, help="consent language, e.g. ru-RU (see consent-text --list)")
    vr.add_argument("--alias", required=True, help="local name for the voice (a-z 0-9 _ -)")
    vr.add_argument("--store", action=argparse.BooleanOptionalAction, default=False,
                    help="--store keeps the voice at Google (voice_..., 1 year, 200 per project); "
                         "--no-store (default) returns a voicekey_... kept locally (7 days)")
    vr.add_argument("--model", default="flash-lite", help="flash-lite (default), flash, or a full model name")
    vr.add_argument("--i-am-the-speaker", action="store_true",
                    help="required: you are the speaker in both recordings and consent to the voice model")
    vr.add_argument("--accept-unpaid-terms", action="store_true",
                    help="proceed without a confirmed billing-linked project (free tier: content may be used)")
    vr.add_argument("--audition-out-dir", help="absolute dir (outside the repo) for a short check clip")
    vr.add_argument("--dry-run", action="store_true", help="preflight the clips and print the request; send nothing")
    vr.set_defaults(func=cmd_voices_replicate)

    vd = vs.add_parser("delete", parents=[common], help="delete a voice by local alias or voice_... id")
    vd.add_argument("name")
    vd.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    vd.add_argument("--dry-run", action="store_true", help="show what would be deleted; change nothing")
    vd.set_defaults(func=cmd_voices_delete)

    t = sub.add_parser("tts", parents=[common], help="text -> speech (WAV by default)")
    src = t.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="inline text")
    src.add_argument("--text-file", help="path to a UTF-8 text file ('-' for stdin)")
    t.add_argument("--voice", help="prebuilt name (Kore), local alias, voice_... id or voicekey_... key")
    t.add_argument("--speaker", action="append", metavar="NAME=VOICE",
                   help="dialogue mode: repeat per speaker; the script uses 'NAME: text' lines, one request per turn")
    t.add_argument("--style", help="turn-level delivery, e.g. 'calm, warm' (put directions here, not in the text)")
    t.add_argument("--model", default=tts.DEFAULT_MODEL, help="flash-lite (default), flash, or a full model name")
    t.add_argument("--lang", help="optional BCP-47 hint (the model detects the language by itself)")
    t.add_argument("--out-dir", required=True, help="absolute output dir, outside the repo")
    t.add_argument("--name", help="output base name (slug); default derives from a hash of the text")
    t.add_argument("--format", choices=tts.OUTPUT_FORMATS, default="wav")
    t.add_argument("--max-chars", type=int, default=tts.DEFAULT_MAX_CHARS, help="chunk size for long scripts")
    t.add_argument("--gap-ms", type=int, default=tts.DEFAULT_GAP_MS, help="silence between stitched chunks")
    t.add_argument("--overwrite", action="store_true")
    t.add_argument("--dry-run", action="store_true", help="print the first request (text elided) and a cost estimate")
    t.set_defaults(func=cmd_tts)

    s = sub.add_parser("stt", parents=[common], help="transcribe an audio file (optional diarization and word timestamps)")
    s.add_argument("--input", required=True, help="absolute path to the audio file, outside the repo")
    s.add_argument("--lang", action="append", help="BCP-47 hint, repeatable (default: auto-detect)")
    s.add_argument("--diarize", action="store_true", help="speaker labels (spk_1, ...), up to 8 speakers, max 30 min")
    s.add_argument("--words", action="store_true", help="word timestamps, max 30 min (may reduce accuracy)")
    s.add_argument("--smart", action="store_true", help="smart mode: no disfluencies, formatted numbers")
    s.add_argument("--term", action="append", help="custom vocabulary term, repeatable")
    s.add_argument("--vocab-file", help="custom vocabulary, one term per line")
    s.add_argument("--format", action="append", choices=stt.TRANSCRIPT_FORMATS,
                   help="output format, repeatable (default txt); srt/vtt need --words")
    s.add_argument("--model", default=voices.MODEL_TRANSCRIBE)
    s.add_argument("--out-dir", required=True, help="absolute output dir, outside the repo")
    s.add_argument("--name", help="output base name (slug); default derives from a hash of the audio")
    s.add_argument("--accept-unpaid-terms", action="store_true",
                   help="proceed without a confirmed billing-linked project (free tier: content may be used)")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="preflight the audio and print the request; send nothing")
    s.set_defaults(func=cmd_stt)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.api not in APIS:
            raise UsageError(f"{ENV_API}={args.api!r} is not one of: {', '.join(APIS)}")
        return args.func(args)
    except AIStudioError as exc:
        print(f"error: {config.redact(str(exc))}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, always redacted
        print(f"error: unexpected {type(exc).__name__}: {config.redact(str(exc))}", file=sys.stderr)
        if args.verbose:
            print(config.redact(traceback.format_exc()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
