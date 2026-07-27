#!/usr/bin/env bash
# Generate remaining hyperstaranie frames sequentially (gaia single-session rule).
# Run from /Users/suenot/projects/sdvg/gaia
# Usage: bash prompts/hyperstaranie/run_all.sh 02 16
set -u
cd /Users/suenot/projects/sdvg/gaia

START="${1:-02}"
END="${2:-16}"
DIR="prompts/hyperstaranie"

for n in $(seq -f "%02g" "$START" "$END"); do
  prompt="$DIR/s${n}.txt"
  log="$DIR/gen_s${n}.log"
  if [[ ! -f "$prompt" ]]; then
    echo "SKIP s${n}: no prompt file"
    continue
  fi
  echo "=== generating s${n} ($(date +%H:%M:%S)) ==="
  before=$(ls output/*.png 2>/dev/null | wc -l | tr -d ' ')
  venv/bin/python3 gemini_image_gen.py --prompt-file "$prompt" --debug > "$log" 2>&1
  code=$?
  after=$(ls output/*.png 2>/dev/null | wc -l | tr -d ' ')
  echo "s${n} exit=$code png_count ${before}->${after} ($(date +%H:%M:%S))"
  # bind latest png to scene name
  latest=$(ls -t output/*.png 2>/dev/null | head -1)
  if [[ -n "$latest" && "$after" -gt "$before" ]]; then
    cp "$latest" "$DIR/frame_s${n}.png"
    echo "bound: $DIR/frame_s${n}.png <- $(basename "$latest")"
  else
    echo "WARN s${n}: no new png, check $log"
  fi
  sleep 4
done
echo "=== ALL DONE ==="
ls -la "$DIR"/frame_s*.png 2>/dev/null
