#!/usr/bin/env bash
# Crop each frame_sNN.png to exact 1080x1920 (9:16) for Shorts.
# Source is ~572x1024 (already near 9:16). Scale up + crop sliver.
set -u
cd /Users/suenot/projects/sdvg/gaia/prompts/hyperstaranie
mkdir -p _916
for f in frame_s*.png; do
  [[ -f "$f" ]] || continue
  out="_916/${f%.png}_916.png"
  ffmpeg -y -loglevel error -i "$f" \
    -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" \
    "$out" && echo "ok: $out"
done
echo "--- done ---"
ls -la _916/
