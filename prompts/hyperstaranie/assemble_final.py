#!/usr/bin/env python3
"""Assemble final Shorts MP4s: narration is master clock, frames timed to audio.
Per scene: frame shown for (audio_duration + tail), xfade between scenes.
Outputs final_part1.mp4 (s01-s08), final_part2.mp4 (s09-s16) with narration.
"""
import subprocess, json, sys
from pathlib import Path

ROOT = Path("/Users/suenot/projects/sdvg/gaia/prompts/hyperstaranie")
FRAMES = ROOT / "_916"        # frame_sNN_916.png
AUDIO  = ROOT / "audio"       # sNN.wav
OUT    = ROOT
TAIL   = 0.45                 # extra sec after each narration before next cut
FADE   = 0.35                 # xfade duration

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
        d = dur(a) + TAIL
        clips.append((f, d, a))
    # build ffmpeg: one looped image input per scene + one audio input per scene
    args = []
    for f, d, _ in clips:
        args += ["-loop", "1", "-t", f"{d:.3f}", "-i", str(f)]
    for _, _, a in clips:
        args += ["-i", str(a)]
    n = len(clips)
    # video xfade chain
    vf = []
    prev_total = clips[0][1]
    prev_label = "[0:v]"
    for i in range(1, n):
        offset = prev_total - FADE
        out_lbl = f"[v{i}]" if i < n - 1 else "[vout]"
        vf.append(f"{prev_label}[{i}:v]xfade=transition=fade:duration={FADE}:offset={offset:.3f}{out_lbl}")
        prev_label = out_lbl
        prev_total += clips[i][1] - FADE
    # audio: concat with small gaps via adelay then amix, simpler: concat
    # inputs n..2n-1 are audio; join sequentially
    a_idx = list(range(n, 2 * n))
    concat_a = "".join(f"[{i}:a]" for i in a_idx)
    af = [f"{concat_a}concat=n={n}:v=0:a=1[aout]"]
    fc = ";".join(vf + af)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + args + [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    total = prev_total
    print(f"→ {out_path.name}  {n} scenes  ~{total:.1f}s")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDERR:", r.stderr[-1200:]); return False
    return True

ok = True
ok &= build([f"s{i:02d}" for i in range(1, 9)], OUT / "final_part1.mp4")
ok &= build([f"s{i:02d}" for i in range(9, 17)], OUT / "final_part2.mp4")
print("DONE" if ok else "FAILED")
sys.exit(0 if ok else 1)
