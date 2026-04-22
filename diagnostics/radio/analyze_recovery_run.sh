#!/usr/bin/env bash
#
# analyze_recovery_run.sh — summarize a recovery_characterization.py JSONL log.
#
# Usage:
#   ./analyze_recovery_run.sh <path-to-run.jsonl>
#
# Reads a JSONL log produced by diagnostics/radio/recovery_characterization.py
# and prints a multi-section summary: run metadata, event-kind histogram,
# latency statistics, timeout details and hourly clustering, slow-probe
# details, and any hang/recovery events. Safe to run on a completed run or
# a still-growing file (jq handles both).
#
# Depends on: jq, awk, python3, stdlib statistics (no pip installs needed).

set -eu

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <path-to-recovery_*.jsonl>" >&2
    exit 1
fi

FILE="$1"

if [[ ! -f "$FILE" ]]; then
    echo "error: file not found: $FILE" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "error: jq not installed" >&2
    exit 1
fi

echo "============================================================"
echo "Recovery run analysis: $FILE"
echo "============================================================"
echo

# ---------------------------------------------------------------------------
# Run metadata: first/last timestamp, span, mode, completion status
# ---------------------------------------------------------------------------
# Checks whether the run completed (has a 'shutdown' event) or was
# interrupted. Computes wall-clock span from first to last probe.

echo "## Run metadata"
echo

STARTUP=$(jq -c 'select(.kind=="startup")' "$FILE" | head -1)
MODE=$(jq -c 'select(.kind=="mode")' "$FILE" | head -1)
SHUTDOWN=$(jq -c 'select(.kind=="shutdown")' "$FILE" | head -1)

if [[ -n "$STARTUP" ]]; then
    echo "  startup: $(echo "$STARTUP" | jq -c '{ts, serial_port, self_info: (.self_info // {} | {name, public_key: (.public_key[0:16] // null)})}')"
fi

if [[ -n "$MODE" ]]; then
    echo "  mode:    $(echo "$MODE" | jq -c 'del(.kind, .elapsed_since_last_healthy_sec)')"
fi

if [[ -n "$SHUTDOWN" ]]; then
    echo "  status:  COMPLETED"
    echo "  shutdown: $(echo "$SHUTDOWN" | jq -c 'del(.kind, .elapsed_since_last_healthy_sec)')"
else
    echo "  status:  INCOMPLETE (no shutdown event — still running or killed)"
fi

# Wall-clock span from first probe to last probe (not startup/shutdown, because
# those bracket the actual polling).
jq -r 'select(.kind=="probe" or .kind=="probe_timeout") | .ts' "$FILE" \
  | awk 'NR==1 {first=$1} END {
      span=$1-first;
      h=int(span/3600); m=int((span%3600)/60); s=span%60;
      printf "  probe span: %ds (%dh%dm%ds)  first=%d  last=%d\n", span, h, m, s, first, $1
    }'
echo

# ---------------------------------------------------------------------------
# Event-kind histogram
# ---------------------------------------------------------------------------
# Every line has a 'kind' field; counting them tells you at a glance what
# happened. In a clean run you expect: 1 startup, 1 mode, many probes, a
# few probe_timeouts, 1 shutdown, and no hang/recovery events.

echo "## Event-kind histogram"
echo
jq -r '.kind' "$FILE" | sort | uniq -c | sort -rn | sed 's/^/  /'
echo

# ---------------------------------------------------------------------------
# Result-type histogram (for probe lines only)
# ---------------------------------------------------------------------------
# Probes resolve to ok / timeout / error. An all-ok run is ideal; a few
# timeouts is normal; errors indicate something unusual (not just slow).

echo "## Result-type histogram (probes only)"
echo
jq -r 'select(.kind=="probe" or .kind=="probe_timeout") | .result_type' "$FILE" \
  | sort | uniq -c | sort -rn | sed 's/^/  /'
echo

# ---------------------------------------------------------------------------
# Latency statistics (successful probes only)
# ---------------------------------------------------------------------------
# Baseline latency is ~5-6ms on a healthy radio (see T9 findings). p99 that
# stays close to p50 means the radio is rock-solid. Elevated max or p99
# indicates occasional slow responses — slower-than-usual but not timed out.

echo "## Latency statistics (successful probes)"
echo

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

jq -r 'select(.kind=="probe" and .result_type=="ok") | .latency_ms' "$FILE" > "$TMPFILE"

python3 - "$TMPFILE" <<'PYEOF'
import sys
path = sys.argv[1]
vals = [float(l) for l in open(path) if l.strip()]
if not vals:
    print("  (no successful probes)")
    sys.exit(0)
vals.sort()
n = len(vals)
def pct(p):
    idx = min(int(n * p), n - 1)
    return vals[idx]
print(f"  n      = {n}")
print(f"  min    = {vals[0]:>8.2f} ms")
print(f"  p50    = {pct(0.50):>8.2f} ms")
print(f"  p95    = {pct(0.95):>8.2f} ms")
print(f"  p99    = {pct(0.99):>8.2f} ms")
print(f"  p99.9  = {pct(0.999):>8.2f} ms")
print(f"  max    = {vals[-1]:>8.2f} ms")
PYEOF
echo

# ---------------------------------------------------------------------------
# Timeouts: hourly clustering
# ---------------------------------------------------------------------------
# Bucket timeouts by hour-since-run-start. Uniform distribution = random
# transient issues. Clustering = episodic problems (mesh burst, GC pass,
# thermal event, etc).

echo "## Timeouts by hour-since-start"
echo

START_TS=$(jq -r 'select(.kind=="probe" or .kind=="probe_timeout") | .ts' "$FILE" | head -1)

TIMEOUT_COUNT=$(jq -r 'select(.result_type=="timeout") | .ts' "$FILE" | wc -l)
if [[ "$TIMEOUT_COUNT" -eq 0 ]]; then
    echo "  (no timeouts)"
else
    jq -r 'select(.result_type=="timeout") | .ts' "$FILE" \
      | awk -v start="$START_TS" '{print int(($1-start)/3600)}' \
      | sort -n | uniq -c \
      | awk '{printf "  hour %2d: %s\n", $2, $1}'
fi
echo

# ---------------------------------------------------------------------------
# Timeout details
# ---------------------------------------------------------------------------
# Full listing of every timeout with its consecutive count. Rising
# consecutive counts mean the radio might be escalating toward a real hang;
# all-1s mean each timeout was isolated and the next poll recovered.

echo "## Timeout details (ts, consecutive_timeouts)"
echo
if [[ "$TIMEOUT_COUNT" -eq 0 ]]; then
    echo "  (no timeouts)"
else
    jq -c 'select(.kind=="probe_timeout") | {ts, consecutive_timeouts}' "$FILE" | sed 's/^/  /'
fi
echo

# ---------------------------------------------------------------------------
# Slow-but-not-timed-out probes
# ---------------------------------------------------------------------------
# Probes above 100ms but below the command timeout. These are "radio is
# alive but slow" events — often clustered near timeouts. They indicate
# the firmware is busy, not dead. The May 19 no_event_received errors
# probably looked like this from the send side.

echo "## Slow successful probes (latency > 100ms)"
echo

SLOW_COUNT=$(jq -c 'select(.kind=="probe" and .latency_ms > 100)' "$FILE" | wc -l)
echo "  count: $SLOW_COUNT"
if [[ "$SLOW_COUNT" -gt 0 ]]; then
    jq -c 'select(.kind=="probe" and .latency_ms > 100) | {ts, latency_ms}' "$FILE" \
      | head -20 | sed 's/^/  /'
    if [[ "$SLOW_COUNT" -gt 20 ]]; then
        echo "  ... ($((SLOW_COUNT - 20)) more)"
    fi
fi
echo

# ---------------------------------------------------------------------------
# Elapsed-since-last-healthy spikes
# ---------------------------------------------------------------------------
# Every line carries elapsed_since_last_healthy_sec. In a healthy run this
# hovers at the polling interval (~2s). Values of 4+ indicate the radio
# went quiet for at least two polls. Useful for spotting dips that didn't
# reach the full-hang threshold.

echo "## Elapsed-since-last-healthy > 3s"
echo
ELAPSED_COUNT=$(jq -c 'select(.elapsed_since_last_healthy_sec != null and .elapsed_since_last_healthy_sec > 3)' "$FILE" | wc -l)
echo "  count: $ELAPSED_COUNT"
if [[ "$ELAPSED_COUNT" -gt 0 ]]; then
    jq -c 'select(.elapsed_since_last_healthy_sec != null and .elapsed_since_last_healthy_sec > 3) | {ts, kind, elapsed_since_last_healthy_sec}' "$FILE" \
      | head -10 | sed 's/^/  /'
    if [[ "$ELAPSED_COUNT" -gt 10 ]]; then
        echo "  ... ($((ELAPSED_COUNT - 10)) more)"
    fi
fi
echo

# ---------------------------------------------------------------------------
# Hang / recovery events
# ---------------------------------------------------------------------------
# Anything interesting: silence_detected, recovery_attempt_*, full_ladder_failed,
# giving_up, port_disappeared_during_polling. On a clean run these are all
# empty. When they happen, they're the main story.

echo "## Hang / recovery events"
echo
HANG_COUNT=$(jq -c 'select(.kind | IN("silence_detected", "silence_investigation", "recovery_attempt_start", "recovery_attempt_end", "recovery_succeeded", "recovery_failed", "full_ladder_failed", "giving_up", "port_disappeared_during_polling"))' "$FILE" | wc -l)
echo "  count: $HANG_COUNT"
if [[ "$HANG_COUNT" -gt 0 ]]; then
    jq -c 'select(.kind | IN("silence_detected", "silence_investigation", "recovery_attempt_start", "recovery_attempt_end", "recovery_succeeded", "recovery_failed", "full_ladder_failed", "giving_up", "port_disappeared_during_polling"))' "$FILE" \
      | sed 's/^/  /'
fi
echo

echo "============================================================"
echo "Done."
echo "============================================================"
