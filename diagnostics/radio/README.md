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

---

## Standalone diagnostics

In addition to the `run_test.py` t0–t4 harness above, this directory
contains single-purpose CLIs for ad-hoc investigation against a connected
Heltec. Each takes `--config config.toml` to find the serial port (no
`/dev/ttyUSB0` assumption); all refuse to run while `mesh_bot.service`
is active and will not stop services for you. Pipe to `tee` to keep a log.

**Writing a new diagnostic that observes inbound DMs?** You MUST call
`await mc.start_auto_message_fetching()` after connect, or
`CONTACT_MSG_RECV` will never fire — the firmware queues DMs in an in-RAM
`offline_queue` and only sends `MESSAGES_WAITING` tickles until the host
drains. See the "MeshCore inbound DM drain" section in `AGENTS.md` for
the protocol, and `drain_queue.py` below for the minimal reference.

### `device_info.py`

Dumps firmware version + build date + `self_info`. Use to confirm which
MeshCore build is running and whether you're on the latest
`meshcore-dev/MeshCore` tag.

```bash
uv run python3 diagnostics/radio/device_info.py --config config.toml
```

### `contacts_purge.py`

Disables firmware auto-add (`set_manual_add_contacts(True)` +
`set_autoadd_config(0)`) and bulk-removes every contact in the firmware
table. The durable fix for the 350-row cap is `mesh_bot._evict_one_contact`
(three-tier LRU, runs on `ERR_CODE_TABLE_FULL`; see AGENTS.md
"Firmware contact-table eviction"); this script is the bulk-reset
escape hatch for when an operator wants to wipe everything and start
clean. Dry-run by default; pass `--yes-really` to actually mutate.

```bash
uv run python3 diagnostics/radio/contacts_purge.py --config config.toml             # preview
uv run python3 diagnostics/radio/contacts_purge.py --config config.toml --yes-really
```

### `drain_queue.py`

Subscribes to `CONTACT_MSG_RECV` and calls `start_auto_message_fetching()`
to drain the firmware's in-RAM `offline_queue[16]` of decoded-but-not-yet-
surfaced DMs. Use when the phone says "delivered" but DMs never reach
mesh_bot — almost always a host-side drain miss rather than a firmware
issue. See CIV-106 for the queue/drain protocol.

```bash
uv run python3 diagnostics/radio/drain_queue.py --config config.toml --window 15
```

### `dm_diagnose.py`

Three-check diagnostic for "I sent a DM to CONTACT and never got an ACK":
verifies the stored contact pubkey against a captured ADVERTISEMENT,
sends a DM and waits for the ACK (with RX-during-wait logging), and runs
path discovery. CONTACT is matched by `adv_name` then by pubkey prefix.

```bash
uv run python3 diagnostics/radio/dm_diagnose.py --config config.toml Fremonster
```

### `dm_rx_trace.py`

Passive observer: subscribes to `RX_LOG_DATA`, `CONTACT_MSG_RECV`, `ACK`,
and `CHANNEL_MSG_RECV` for a configurable window and correlates inbound
TXT_MSG packets with subsequent decode events. Use when you can see
RX activity but DMs aren't surfacing and want to localise where in the
firmware pipeline they die. Auto-drains the offline_queue.

```bash
uv run python3 diagnostics/radio/dm_rx_trace.py --config config.toml --window 120
```

### `manual_decrypt.py`

Bypasses the firmware entirely: exports the Pi's X25519 private key,
performs ECDH + AES-128-ECB + HMAC-SHA-256 in Python, and decrypts a
captured inbound TXT_MSG ciphertext. The definitive "is the crypto
math right?" test. Self-tests against RFC 7748 §6.1 vectors at startup.

Note: `self_info.public_key` is the Ed25519 identity pubkey; the firmware
converts to X25519 via Edwards→Montgomery inside `getSharedSecret`. This
tool does the same conversion — handy reference if you ever need to
operate on MeshCore keys outside the firmware.

```bash
uv run python3 diagnostics/radio/manual_decrypt.py --config config.toml \
    --cipher-hex <raw-hex-from-dm_rx_trace-log>     # offline decrypt
uv run python3 diagnostics/radio/manual_decrypt.py --config config.toml --capture
```

### `minimum_contact_probe.py`

Adds a MeshCore contact with only a pubkey + sensible defaults (empty
`adv_name`, `type=1`, `flags=0`, `out_path_len=-1`), then runs a TX leg
(send DM from hub, wait for ACK) and an RX leg (operator sends DM from
the phone, count `CONTACT_MSG_RECV`). PASS on both legs means the
captive-portal contact-add form needs only a pubkey field; the local
`adv_name` is purely cosmetic for the hub's own UI.

```bash
uv run python3 diagnostics/radio/minimum_contact_probe.py \
    --config config.toml \
    --pubkey <64-hex-chars>                  # the experiment: empty name
uv run python3 diagnostics/radio/minimum_contact_probe.py \
    --config config.toml --pubkey <hex> --name <name>   # baseline
```
