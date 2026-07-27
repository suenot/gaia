#!/usr/bin/env python3
"""Assemble hyperstaranie frames into 2 preview MP4s (9:16, crossfade, no audio)."""
import subprocess, sys
from pathlib import Path

D = Path("/Users/suenot/projects/sdvg/gaia/prompts/hyperstaranie/_916")
OUT = D.parent

def build(frames, out_path, clip=4.0, fade=0.6):
    # inputs
    args = []
    for f in frames:
        args += ["-loop", "1", "-t", str(clip), "-i", str(f)]
    # filter: xfade chain
    # offset for i-th transition = clip*(i+1) - fade  (since each clip full length)
    # but with xfade, durations overlap: total = clip + (n-1)*(clip-fade)
    filt = []
    prev = "[0:v]"
    # xfade chain: offset_i = clip + (i-1)*(clip-fade) - fade
    for i in range(1, len(frames)):
        offset = clip + (i - 1) * (clip - fade) - fade
        out_label = f"[v{i}]" if i < len(frames) - 1 else "[vout]"
        filt.append(f"{prev}[{i}:v]xfade=transition=fade:duration={fade}:offset={offset}{out_label}")
        prev = out_label
    filter_complex = ";".join(filt)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + args + [
        "-filter_complex", filter_complex,
        "-map", "[vout]" if len(frames) > 1 else "[0:v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", "30", "-movflags", "+faststart",
        str(out_path),
    ]
    print("→", out_path.name, f"({len(frames)} frames)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDERR:", r.stderr[-800:])
        sys.exit(1)

frames = sorted(D.glob("frame_s*.png"))
part1 = frames[:8]
part2 = frames[8:]
build(part1, OUT / "preview_part1.mp4")
build(part2, OUT / "preview_part2.mp4")
print("DONE")
