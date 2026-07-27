#!/usr/bin/env python3
"""Assemble final Shorts MP4s. Narration-paced, with breathing gaps + smooth fades.

Per scene: frame shown for (audio_duration + GAP). Between scenes: xfade FADE sec.
Audio: each phrase delayed so it starts when its frame is fully visible, with
GAP silence after it (breathing). Compensates xfade overlap.
"""
import subprocess, json, sys
from pathlib import Path

ROOT = Path("/Users/suenot/projects/sdvg/gaia/prompts/hyperstaranie")
FRAMES = ROOT / "_916"
AUDIO  = ROOT / "audio"
OUT    = ROOT
GAP    = 0.80    # silence after each phrase before next cut (breathing)
FADE   = 0.60    # xfade duration

def dur(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)], capture_output=True, text=True)
    return float(json.loads(r.stdout)["format"]["duration"])

def build(scene_ids, out_path):
    clips = []
    for sid in scene_ids:
        a = AUDIO / f"{sid}.wav"
        f = FRAMES / f"frame_{sid}_916.png"
        if not a.exists() or not f.exists():
            print(f"MISSING {sid}: audio={a.exists()} frame={f.exists()}", file=sys.stderr)
            return False
        ad = dur(a)
        clips.append((f, a, ad, ad + GAP))  # (frame, audio, audio_dur, frame_dur)
    n = len(clips)

    # inputs: n looped images + n audios
    args = []
    for f, _, _, fd in clips:
        args += ["-loop", "1", "-t", f"{fd:.3f}", "-i", str(f)]
    for _, a, _, _ in clips:
        args += ["-i", str(a)]

    # VIDEO: hard cut (concat) with per-frame fadein/fadeout = dip-through-black.
    # Cleaner than xfade (no double-exposure flicker on dissimilar frames).
    vf = []
    total_v = 0.0
    for i, (f, _, _, fd) in enumerate(clips):
        fi = 0.18
        fo = 0.18
        fo_st = max(0.0, fd - fo)
        vf.append(
            f"[{i}:v]format=yuv420p,scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,fade=t=in:st=0:d={fi},fade=t=out:st={fo_st:.3f}:d={fo}[v{i}]"
        )
        total_v += fd
    concat_in = "".join(f"[v{i}]" for i in range(n))
    vf.append(f"{concat_in}concat=n={n}:v=1:a=0[vcat]")
    vf.append(f"[vcat]fade=t=in:st=0:d=0.3,fade=t=out:st={total_v-0.4:.3f}:d=0.4[vfin]")

    # AUDIO: each phrase at its frame's start (concat model = simple accumulation).
    af = []
    start = 0.0
    for i, (_, _, _, fd) in enumerate(clips):
        ms = int(round(start * 1000))
        af.append(f"[{n + i}:a]adelay={ms}|{ms}[a{i}]")
        start += fd
    amix_inputs = "".join(f"[a{i}]" for i in range(n))
    af.append(f"{amix_inputs}amix=inputs={n}:duration=longest:normalize=0[aflat]")
    af.append(f"[aflat]afade=t=in:st=0:d=0.2,afade=t=out:st={total_v-0.4:.3f}:d=0.4[aout]")

    fc = ";".join(vf + af)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + args + [
        "-filter_complex", fc,
        "-map", "[vfin]", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"-> {out_path.name}  {n} scenes  ~{total_v:.1f}s  (GAP={GAP}, dip={0.18})")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDERR:", r.stderr[-1500:]); return False
    return True

ok = True
ok &= build([f"s{i:02d}" for i in range(1, 9)], OUT / "final_part1.mp4")
ok &= build([f"s{i:02d}" for i in range(9, 17)], OUT / "final_part2.mp4")
print("DONE" if ok else "FAILED")
sys.exit(0 if ok else 1)
