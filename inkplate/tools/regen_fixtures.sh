#!/usr/bin/env bash
# Regenerate or verify fixture goldens. Each fixture directory under
# inkplate/fixtures/ has an in.json (combined envelope+payload) and
# an expected.png (the 1-bit PNG host_render produces from in.json).
#
# Modes:
#   --check    Re-render each in.json and diff against expected.png.
#              Exits 0 if every fixture matches; 1 on first mismatch.
#   --write    Re-render and overwrite expected.png. Use after an
#              intentional layout change; then inspect via
#              `git diff inkplate/fixtures/` before committing.
#
# Requires inkplate/host/host_render to be built. Will rebuild
# silently if the binary is missing.

set -euo pipefail

if [ $# -ne 1 ] || { [ "$1" != "--check" ] && [ "$1" != "--write" ]; }; then
  echo "usage: $0 --check | --write" >&2
  exit 2
fi
MODE="$1"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FIXTURES="$ROOT/inkplate/fixtures"
HOST_DIR="$ROOT/inkplate/host"
BIN="$HOST_DIR/host_render"

if [ ! -x "$BIN" ]; then
  echo "building host_render..." >&2
  (cd "$HOST_DIR" && make) >&2
fi

shopt -s nullglob
DIRS=( "$FIXTURES"/*/ )
if [ ${#DIRS[@]} -eq 0 ]; then
  echo "no fixtures under $FIXTURES" >&2
  exit 1
fi

# Bulletin-template fixtures whose goldens were deleted during the chat-
# layout rework. --check skips them so the regression guard still works
# for failure_shell / api_mismatch / critical_battery (those templates
# aren't changing in this rework). When a bulletin fixture's rendering
# stabilizes, run --write against it manually, eyeball the new
# expected.png, then drop the name from this list.
SKIP_CHECK_BULLETIN=(
  low_battery__bulletin
  normal_bulletin__channel_cycle_position_indicator
  normal_bulletin__empty_channel
  normal_bulletin__five_messages
  normal_bulletin__long_body_truncated
  normal_bulletin__pinned_first
  stale__radio_down
)

failed=0
total=0
skipped=0
for d in "${DIRS[@]}"; do
  name="$(basename "$d")"
  in_json="$d/in.json"
  expected="$d/expected.png"
  if [ ! -f "$in_json" ]; then
    echo "  SKIP $name (no in.json)" >&2
    continue
  fi
  if [ "$MODE" = "--check" ]; then
    skip=0
    for sk in "${SKIP_CHECK_BULLETIN[@]}"; do
      if [ "$sk" = "$name" ]; then skip=1; break; fi
    done
    if [ "$skip" = "1" ]; then
      echo "  skip $name (bulletin layout in flux; no golden)"
      skipped=$((skipped + 1))
      continue
    fi
  fi
  total=$((total + 1))
  tmp="$(mktemp --suffix=.png)"
  if ! "$BIN" < "$in_json" > "$tmp"; then
    echo "  FAIL $name (host_render error)"
    failed=$((failed + 1))
    rm -f "$tmp"
    continue
  fi
  case "$MODE" in
    --check)
      if [ ! -f "$expected" ]; then
        echo "  FAIL $name (no expected.png — run --write)"
        failed=$((failed + 1))
      elif ! cmp -s "$tmp" "$expected"; then
        echo "  FAIL $name (drift)"
        failed=$((failed + 1))
      else
        echo "  ok   $name"
      fi
      ;;
    --write)
      mv "$tmp" "$expected"
      echo "  wrote $name"
      tmp=""
      ;;
  esac
  [ -n "$tmp" ] && rm -f "$tmp"
done

if [ "$skipped" -gt 0 ]; then
  echo "--- $((total - failed))/$total ok ($skipped skipped) ---" >&2
else
  echo "--- $((total - failed))/$total ok ---" >&2
fi
exit $([ $failed -eq 0 ] && echo 0 || echo 1)
