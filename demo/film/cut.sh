#!/usr/bin/env bash
# Turn rendered frames into video: one MP4 per shot, then the shots joined in name order.
#
# Usage: demo/film/cut.sh <film> [fps]
# Reads <film>/frames/<shot>/rgb_NNNN.png (render_isaac.py) and writes <film>/video/<shot>.mp4 and
# <film>/plant7.mp4. Needs ffmpeg.
set -euo pipefail

film=${1:?usage: $0 <film> [fps]}
fps=${2:-30}
command -v ffmpeg >/dev/null || { echo "ffmpeg is not installed" >&2; exit 2; }
mkdir -p "$film/video"
list="$film/video/shots.txt"
: > "$list"
for dir in "$film"/frames/*/; do
  shot=$(basename "$dir")
  # Replicator writes rgb_NNNN.png, sometimes in a nested folder; take them in name order.
  first=$(find "$dir" -name 'rgb_*.png' | sort | head -n 1)
  [[ -n $first ]] || { echo "no frames for $shot; skipping" >&2; continue; }
  ffmpeg -loglevel error -y -framerate "$fps" -pattern_type glob -i "$(dirname "$first")/rgb_*.png" \
    -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p "$film/video/$shot.mp4"
  echo "file '$shot.mp4'" >> "$list"
  echo "$film/video/$shot.mp4"
done
ffmpeg -loglevel error -y -f concat -safe 0 -i "$list" -c copy "$film/plant7.mp4"
echo "$film/plant7.mp4"
