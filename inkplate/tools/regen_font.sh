#!/usr/bin/env bash
# Regenerate all committed LessPerfectDOSVGA GFXfont headers from the TTF.
#
# This delegates to regen_font.py because the dev Pi already has
# python3-freetype (apt) but not libfreetype-dev (which Adafruit's C
# fontconvert needs). The Python implementation produces a
# byte-identical Adafruit GFXfont struct.
#
# Inputs:
#   - inkplate/tools/LessPerfectDOSVGA.ttf  (vendored)
# Outputs (regenerated as a set so a TTF update can't desync them):
#   - inkplate/render/src/fonts/LessPerfectDOSVGA.h    (16 px, symbol LessPerfectDOSVGA)
#   - inkplate/render/src/fonts/LessPerfectDOSVGA24.h  (24 px, symbol LessPerfectDOSVGA24)
#
# To add another size, append a row to the SIZES array and rerun. Then
# regenerate goldens: ../tools/regen_fixtures.sh --write
#
# To update the TTF, replace inkplate/tools/LessPerfectDOSVGA.ttf with
# a fresh download from http://laemeur.sdf.org/fonts/LessPerfectDOSVGA.ttf

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TTF="$ROOT/inkplate/tools/LessPerfectDOSVGA.ttf"
PY="$ROOT/inkplate/tools/regen_font.py"

# Each row: "<px>:<symbol>:<out-filename>"
SIZES=(
  "16:LessPerfectDOSVGA:LessPerfectDOSVGA.h"
  "24:LessPerfectDOSVGA24:LessPerfectDOSVGA24.h"
)

if [ ! -f "$TTF" ]; then
  echo "ERROR: $TTF not found" >&2
  echo "Download from http://laemeur.sdf.org/fonts/LessPerfectDOSVGA.ttf and place here." >&2
  exit 1
fi

for row in "${SIZES[@]}"; do
  IFS=':' read -r px symbol fname <<< "$row"
  out="$ROOT/inkplate/render/src/fonts/$fname"
  python3 "$PY" --ttf "$TTF" --px "$px" --symbol "$symbol" --out "$out"
done
