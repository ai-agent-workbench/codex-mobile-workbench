#!/usr/bin/env bash
# Rasterize our original, geometry-only SVG. Developer-only ImageMagick tool.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
for size in 192 512; do
    convert -background none "$root/static/workbench-icon.svg" -resize "${size}x${size}" -strip "$root/static/icon-${size}.png"
    cp "$root/static/icon-${size}.png" "$root/static/icon-maskable-${size}.png"
done
