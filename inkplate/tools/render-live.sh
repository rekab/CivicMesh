#!/usr/bin/env bash
# Fetch live /api/external-display/state from the dev hub, wrap it in
# a stock envelope, and render to a PNG via host_render.
#
# Usage: render-live.sh [output-path]
# Env:   HUB=http://civicmesh   (default)

set -euo pipefail

HUB="${HUB:-http://civicmesh}"
OUT="${1:-/tmp/civicmesh-frame.png}"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$ROOT/inkplate/host/host_render"

if [ ! -x "$BIN" ]; then
  (cd "$ROOT/inkplate/host" && make) >&2
fi

curl -fsS "$HUB/api/external-display/state" \
  | jq '{envelope: {status:"ok", radio_state:"ok", portal_state:"ok",
                    battery_volts:3.95, seconds_since_last_update:60,
                    active_channel_index:0, failure_reason:null,
                    firmware_version:"0.1.0", expected_api_version:2,
                    cached_payload:null}, payload: .}' \
  | "$BIN" > "$OUT"

echo "wrote: $OUT" >&2
echo "(on the Pi: scp $(hostname):$OUT \$YOUR_DESKTOP/Downloads/ to view)" >&2
