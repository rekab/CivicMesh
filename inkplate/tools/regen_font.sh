#!/usr/bin/env bash
# Regenerate inkplate/render/src/fonts/LessPerfectDOSVGA.h.
#
# This delegates to regen_font.py because the dev Pi already has
# python3-freetype (apt) but not libfreetype-dev (which Adafruit's C
# fontconvert needs). The Python implementation produces a
# byte-identical Adafruit GFXfont struct.
#
# Inputs:
#   - inkplate/tools/LessPerfectDOSVGA.ttf  (vendored)
# Output:
#   - inkplate/render/src/fonts/LessPerfectDOSVGA.h  (committed generated artifact)
#
# To change the native glyph height, edit --px below and rerun. Then
# regenerate goldens: ../tools/regen_fixtures.sh --write
#
# To update the TTF, replace inkplate/tools/LessPerfectDOSVGA.ttf with
# a fresh download from http://laemeur.sdf.org/fonts/LessPerfectDOSVGA.ttf

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TTF="$ROOT/inkplate/tools/LessPerfectDOSVGA.ttf"
OUT="$ROOT/inkplate/render/src/fonts/LessPerfectDOSVGA.h"
PX="${PX:-16}"

if [ ! -f "$TTF" ]; then
  echo "ERROR: $TTF not found" >&2
  echo "Download from http://laemeur.sdf.org/fonts/LessPerfectDOSVGA.ttf and place here." >&2
  exit 1
fi

python3 "$ROOT/inkplate/tools/regen_font.py" \
  --ttf "$TTF" --px "$PX" --out "$OUT"
