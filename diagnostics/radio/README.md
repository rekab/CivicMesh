# CivicMesh Radio Round-Trip Diagnostics Harness

A Mac-side test harness that drives the LoRa radios on two CivicMesh nodes
(pi4, zero2w) directly via the `meshcore` Python library, bypassing the
`mesh_bot` app layer. Designed to isolate a suspected `meshcore_py` bug
where `send_chan_msg` returns `EventType.ERROR` with
`{'reason': 'no_event_received'}` even though the radio actually
transmitted and the recipient received the packet.

## Prerequisites

- Copy `nodes.toml.example` to `nodes.toml` and fill in your two nodes'
  IPs, SSH user, and on-node repo path. `nodes.toml` is gitignored.
- The driver machine has SSH access (key-based) to both nodes — by
  convention `pi4` lives on your LAN and `zero2w` is reachable only
  over its own captive-portal AP gateway.
- Each node has the CivicMesh repo checked out at the `repo_path` set
  in `nodes.toml`, with a working virtualenv at `.venv/` that has
  `meshcore` installed.
- **Mesh_bot must be stopped on both nodes before running.** The harness
  refuses to run if `/dev/ttyUSB0` is held by another process. Stop via
  tmux:
  ```
  ssh <user>@<pi4-ip>     # then tmux attach, Ctrl-C the mesh_bot pane
  ssh <user>@<zero2w-ip>  # same
  ```
  The harness never starts, stops, or kills mesh_bot itself. Restart
  mesh_bot manually after diagnostics are done.

## Invocation

All from the CivicMesh repo root on the driver machine:

```
python diagnostics/radio/run_test.py t0        # 30s passive listen — checks RX on both nodes
python diagnostics/radio/run_test.py t1        # single send pi4 → zero2w
python diagnostics/radio/run_test.py t2        # single send zero2w → pi4
python diagnostics/radio/run_test.py t3        # 20 sends each direction
python diagnostics/radio/run_test.py t4        # self-echo characterization (5 sends each direction)
python diagnostics/radio/run_test.py all       # t0, t1, t2, t3 in sequence (T4 is opt-in)
python diagnostics/radio/run_test.py t3 --iterations 40   # longer T3
python diagnostics/radio/run_test.py t4 --iterations 10   # longer T4

python diagnostics/radio/run_test.py t1 --runs-dir /tmp/my-runs
```

**T4 (self-echo characterization)** answers two questions before any
heard-count UI/schema work:

- **Q1**: does the SENDING node hear its own transmissions echoed back via
  local repeaters as `CHANNEL_MSG_RECV` events? If NO, the heard-count
  feature must be built on `RX_LOG_DATA` with manual decryption rather
  than the cleaner `CHANNEL_MSG_RECV` path.
- **Q2**: how long should the application keep an outbound message in
  active echo-matching state before retiring it? T4 produces a
  histogram of echo arrival times and a conservative recommended
  lifetime (`max(observed_max + 5s, 15s floor)`).

Matching uses a strict composite key `(text, sender_timestamp,
txt_type)` rather than a substring search — the design preference is
undercount over false-positive attribution. Read the top of T4's
`summary.md` for the Q1 verdict; it's the load-bearing finding.

Config lives in `diagnostics/radio/nodes.toml`. Edit that, not the Python.

## Output

Each invocation creates a timestamped subdirectory under
`diagnostics/radio/runs/` and updates a `latest` symlink to it:

```
diagnostics/radio/runs/
  2026-04-17T21-15-03__t1_unidirectional_pi4_to_zero2w/
    manifest.json                             # test config + verdict + paths
    summary.md                                # human-readable verdict
    pi4/
      events__<runid>.jsonl                   # streamed JSONL events (every EventType)
      authoritative__<runid>.jsonl            # /tmp/civicmesh_harness_<runid>.jsonl fetched post-run
      meshcore_debug__<runid>.log             # library debug + Python logging output
    zero2w/
      events__<runid>.jsonl
      authoritative__<runid>.jsonl
      meshcore_debug__<runid>.log
  latest -> 2026-04-17T21-15-03__t1_unidirectional_pi4_to_zero2w/
```

T3 launches two subprocess batches (one per direction), so each node
directory contains two sets of files — one per `<runid>`.

### Reading `events.jsonl`

One JSON object per line. Every object has:

```json
{
  "wall_ts": "2026-04-17T21:15:03.472Z",
  "mono_ts": 12345.678,
  "node": "pi4",
  "event_type": "CHANNEL_MSG_RECV",
  "seq": 42,
  "payload": { ... }
}
```

`mono_ts` is per-node monotonic — do NOT compare it across nodes. Use
`wall_ts` for cross-node correlation (but see the clock-skew caveat below).

Key event types the harness emits directly (not from the library):

- `HARNESS_START`, `HARNESS_INIT` — parameters, enumerated EventType names,
  meshcore version.
- `SELF_INFO` — the `mc.self_info` dict populated by `send_appstart`
  (radio params, node name).
- `PRE_STATS`, `POST_STATS` — `send_device_query` / `get_bat` /
  `send_cmd` probes (each records `not_available` if missing).
- `SUBSCRIBED` — count of EventTypes we subscribed to; any errors.
- `SEND_PRE`, `SEND_RESULT` (the critical one), `SEND_EXCEPTION`.
- `RADIO_STALLED` — 30+ seconds of silence on a connected radio.
- `TEST_COMPLETE` — sentinel for clean shutdown.

All other `event_type` values come from `meshcore.EventType` — the library
decides its own names. The harness does NOT hardcode which event types
exist; the `HARNESS_INIT` record enumerates them at runtime.

## Verdicts (T1, T2, T3)

- **`BOTH_AGREE_SENT`** — sender reported OK, recipient received. Happy
  round trip.
- **`BOTH_AGREE_FAILED`** — sender reported ERROR, recipient got nothing.
  Honest failure.
- **`SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED`** — ⚠️ the bug we're
  hunting. Sender returned ERROR but the packet actually made it.
- **`SENDER_SAYS_SENT_NOT_RECEIVED`** — sender reported OK but recipient
  never saw it. Sent cleanly, lost on air (or decode failure).
- **`INCONCLUSIVE`** — missing data. Open `events.jsonl` to investigate.

T0 has no verdict; it's a passive listen that reports packets-heard per
node and flags an asymmetry if one node hears ≥10× fewer packets.

## Known limitations

- Third-party traffic on `#civicmesh` (repeaters, other operators, the
  user's phone) is recorded rather than filtered. Verdict matching uses
  unique per-run marker UUIDs, so unrelated traffic doesn't confuse the
  classification — but the total event counts will include it.
- Cross-node `mono_ts` math is meaningless. Wall-clock latency is
  annotated per test but accurate only to within NTP skew between the
  two nodes; each run records `chronyc tracking` output and an SSH
  round-trip skew estimate in `manifest.json` and `summary.md`.
- The event type names `RX_LOG_DATA` and `ADVERTISEMENT` mentioned in
  the test-design spec may differ in the installed library version. The
  harness enumerates `list(EventType)` at runtime and records the exact
  set in `HARNESS_INIT` so you can see what this library exposes.
- T3's 20 iterations won't surface rare false-ERRORs (rate <5%). If a
  clean T3 is suspicious, re-run with `--iterations 40` or more.
- The harness reads the node's `config.toml` on each node for the
  channel index. If the two nodes disagree on channel ordering, the
  preflight check flags the mismatch in `summary.md` — but does NOT
  abort. Each node uses its own locally correct index.
- No Mac-side `meshcore` install is required; the library only runs on
  the nodes.

## What the harness will NOT do

- Mutate radio config (`set_radio`, `set_channel`, `set_name`). Only
  reads `self_info`. Calling `set_radio` unnecessarily is known to break
  sessions on firmware v1.11.0.
- Write to SQLite on either node.
- Start, stop, or kill mesh_bot. Refuses to run if mesh_bot is up.
- `pip install` anything remotely. Import failures abort preflight.
- Modify any CivicMesh source outside `diagnostics/radio/`.
- Run itself on a schedule or after delivery. First real run is manual.
