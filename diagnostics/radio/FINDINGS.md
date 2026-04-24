# Radio Diagnostics — Findings

Cross-run summary of what the T0–T4 harness has revealed about the LoRa
stack and how it should inform CivicMesh feature design. Update this
file as new findings emerge; the run directories themselves are
git-ignored, so this is the durable record.

## meshcore_py library: `send_chan_msg` false-ERROR (T1, T3)

**Symptom**: `mc.commands.send_chan_msg` returned
`EventType.ERROR` with `{'reason': 'no_event_received'}` on a
non-trivial fraction of sends — even when the packet transmitted and
the receiving node decoded it cleanly.

**Mechanism**: `meshcore_py` waits up to 5 seconds for an `OK`
command-response event from the firmware after `send_chan_msg`. When
the firmware's response arrives outside that window (under sustained
send load), the library returns ERROR despite a successful
transmission.

**Resolution**: bug is fixed in `meshcore==2.3.6`. CivicMesh's
`pyproject.toml` is now strictly pinned to that version; see the
comment block there for the rationale and re-test procedure.

## CivicMesh channel-secret derivation

`_setup_mesh_client` in `mesh_bot.py` derives the shared channel secret as
`sha256(channel_name)[:16]` and asserts it via
`mc.commands.set_channel(idx, name, secret_bytes)` on every startup.
Any code path that opens a fresh meshcore session and expects channel
messages to round-trip must do the same — otherwise messages may
transmit at the PHY level (RX_LOG_DATA fires on the recipient) but
fail to decrypt and never become CHANNEL_MSG_RECV. The radio
diagnostics harness mirrors this.

## meshcore_py API: `mc.commands.*` for sends/queries

All packet-sending and device-querying methods live under
`mc.commands.*`, never on the instance directly. Examples:

  - `await mc.commands.send_chan_msg(idx, text)`
  - `await mc.commands.send_device_query()`
  - `await mc.commands.get_bat()`
  - `await mc.commands.get_channel(idx)`
  - `await mc.commands.set_channel(idx, name, secret_bytes)`

Instance-level methods that stay on `mc` directly: `subscribe`,
`start_auto_message_fetching`, `disconnect`, `get_contact_by_name`,
plus the `self_info` attribute. Writing `mc.send_chan_msg(...)`
silently raises `AttributeError`.

## MeshCore repeater architecture

Repeaters operate at the **LoRa packet level**, beneath the channel
encryption. They re-broadcast every packet they receive on the
configured radio params (subject to TTL / hop limit / loop
prevention). They are NOT subscribed to specific channels and do NOT
filter by channel.

Diagnostic implication: don't reason about repeater behavior in terms
of "is this repeater on channel X" — that concept doesn't apply.

## Firmware behavior: `CHANNEL_MSG_RECV` is deduped (T0, T4)

The Heltec V3 firmware deduplicates packets at the decode stage. When
the same packet arrives multiple times at the RF layer (direct + via
various repeater paths), each arrival fires its own `RX_LOG_DATA`
event, but **only the first arrival** is decoded into a
`CHANNEL_MSG_RECV` event. Subsequent arrivals are silently dropped at
the channel-message layer.

T0 evidence: a single `Fremonster: Test` message produced 6
`RX_LOG_DATA` events on each receiving node — at `path_len` 0/1/17 —
but only **one** `CHANNEL_MSG_RECV` per node (path_len=0, the first
arrival).

**This is the load-bearing finding for any heard-count feature.** The
implementation cannot use `CHANNEL_MSG_RECV` to count repeats — that
event is firmware-deduped to one decode per packet, regardless of how
many physical receptions occurred.

## Heard-count feature: forced implementation path

Given the firmware-dedup behavior above, the heard-count feature must
be built on `RX_LOG_DATA` with manual decryption:

  - Subscribe to `RX_LOG_DATA` on the sending node.
  - For each event, decrypt the `payload` bytes using the channel
    secret (`sha256(channel_name)[:16]`) — the same secret asserted
    via `set_channel` at startup.
  - Match the decrypted plaintext against locally-originated outbox
    sends using the strict composite key
    `(channel_idx, sender_timestamp, "Name: Text")`.
  - On match: increment `heard_count` on the corresponding outbox
    row, update `min_path_len`, `last_heard_ts`, `best_snr`.

Apply only to **locally-originated outbound messages** (mesh_bot AND
wifi outbox sends, uniformly). Do not add a generic `repeat_count`
column to a shared messages table that would carry meaningless zeros
for received messages.

## Matching-window recommendation (T4, 2026-04-18)

T4's RX_LOG_DATA observations (zero2w batch with active local
repeater):

  - 8 path_len > 0 RX_LOG_DATA events on the sender side, distributed
    across path_len 1, 4, 5 (probable local-repeater rebroadcasts) and
    17, 20, 23 (almost certainly ambient mesh traffic, not our own
    packets bouncing back).
  - Time-offset from nearest preceding `SEND_PRE`: p50=2.26s,
    p95=10.10s, max=12.01s.

Caveat: without payload decryption the harness cannot separate
own-packet rebroadcasts from ambient traffic. The production
implementation does decrypt and so can apply the strict triple-match
key, dropping anything that doesn't decrypt to one of our outbox
plaintexts.

**Recommended matching-window lifetime: 20s.** Observed candidate
echoes maxed at 12s; +5s safety margin is 17s; rounded up to 20s for
headroom, since one quiet test run is not proof of universally short
tails. Late events that don't decrypt to an active outbox row are
dropped without harm — undercount over false-positive.

## Asymmetry: pi4 vs zero2w repeater coverage

In the same T4 run, **all 8 sender-side path_len > 0 events came
from the zero2w batch; pi4 had zero**. Likely reason: the active local
repeater is closer to (or has clearer line-of-sight to) zero2w than
pi4. Worth re-validating from each node's location independently if
heard-counts seem suspiciously low at one site.

## Decryption smoke test (2026-04-18, T4 with `set_decrypt_channel_logs(True)`)

Validated the load-bearing assumption in `docs/heard_count_design.md`:
that we don't need to implement AES ourselves and can rely on
`meshcore_py`'s built-in decryption to populate plaintext fields on
`RX_LOG_DATA` events.

Procedure: temporarily added `mc.set_decrypt_channel_logs(True)` to
`harness/node_side.py` (after `set_channel`/`get_channel` registered
the channel with the parser), re-ran T4, inspected the RX_LOG_DATA
events, then reverted the change. Findings:

**Decryption works on every channel-message RX_LOG_DATA**, populating
the assumed fields:

| field              | populated when             | example value                                  |
|--------------------|----------------------------|------------------------------------------------|
| `message`          | decrypt success            | `"oh_no: [RTTEST] t4 606916b5 pi4->zero2w i=0 u=5c85ae12"` |
| `sender_timestamp` | decrypt success            | `1776479911` (unix epoch s)                    |
| `txt_type`         | decrypt success            | `0` (plain text)                               |
| `chan_name`        | decrypt success            | `"#civicmesh"`                                 |
| `msg_hash`         | decrypt success            | `3149666130` (cache key)                       |
| `attempt`          | decrypt success            | `0`                                            |
| `chan_hash`        | always (1 byte)            | `"b4"`                                         |
| `cipher_mac`       | always (2 bytes)           | `"33ba"`                                       |
| `crypted`          | always (raw ciphertext)    | (hex)                                          |
| `pkt_hash`         | always                     | `1179614920` (32-bit)                          |

Of 37 GRP_TXT RX_LOG_DATA events captured in the run, 27 had
`message` populated — exactly the events whose `chan_hash` matched a
registered channel AND passed the HMAC-MAC validation. The remaining
10 had `chan_hash` from other channels (`f0`, `72`) and `chan_name:
None` — the library correctly recognized them as "not on a channel I
have the secret for" and skipped decryption.

**Subtlety worth knowing**: `chan_hash` is only 1 byte, so collisions
are common (1/256). One zero2w event had `chan_hash=b4` (matches our
`#civicmesh`) but `cipher_mac=33ba` instead of the expected MAC for
our channel — the library correctly rejected the decrypt via HMAC
verification, leaving `message: None`. Production code must NOT match
on `chan_hash` alone; rely on `message != None && chan_name ==
"<expected channel>"`.

**Own-send vs ambient is cleanly separable**:

| batch                      | total path_len>0 sender-side | own-send | ambient | own pkt_hashes | ambient path_lens   |
|----------------------------|------------------------------|----------|---------|----------------|---------------------|
| pi4 → zero2w (sender pi4)  | 7                            | **7**    | 0       | 4 distinct     | (none)              |
| zero2w → pi4 (sender zero2w)| 6                            | **3**    | 3       | 3 distinct     | 5, 6, 24 (far mesh) |

For pi4's batch: 4 unique packets bounced back, with 7 total physical
receptions (so 3 of those 4 packets were heard via 2 different paths
each). Path_len distribution of own-sends: `{1: 4, 2: 2, 3: 1}` —
local repeater plus a couple of two/three-hop paths.

For zero2w's batch: 3 own-send echoes (one per send, each at
path_len=1 — single local repeater hop) plus 3 ambient events at
path_len 5/6/24. The ambient ones are clearly far-mesh flood traffic
from other operators; matching by `message.startswith(f"{my_name}:
")` plus marker text comparison cleanly excludes them.

**Implications for the production design**:

- The proposed implementation in `docs/heard_count_design.md` works
  exactly as designed. No need to revisit the manual-decryption
  fallback path.
- The `ActiveOutboxIndex` matching key `(channel, text, sender_ts)`
  is sufficient given the data — `sender_timestamp` from the
  decrypted payload is monotonic and unique per send.
- `heard_count` semantics: each `RX_LOG_DATA` event with
  `message`/`sender_timestamp` matching is one increment. So the
  count includes physical-reception duplicates of the same packet
  (e.g. pi4 saw pkt_hash 1179614920 twice — that's heard_count += 2).
  This is the right semantic for "how many times my radio heard this
  message," matching what the Android client appears to display.
- One refinement worth considering: also expose `distinct_paths`
  (count of distinct path_len values seen, or count of distinct
  pkt_hashes if dedup is desired). Out of scope for the first
  iteration — `heard_count` + `min_path_len` is enough to get the UI
  off the ground.

The smoke test does not contradict any assumption in the design doc.
Cleared for implementation.

## T9 — Liveness ping latency characterization

**Run:** 2026-04-19 (20:40–21:40 PDT) on civicmesh-toorcamp-01 (Pi Zero 2W), 3601s total, meshcore_py 2.3.6, mesh_bot stopped.
**Radio:** public_key `1864e4638b788f1a…`, name `1864E463`, params 910.525 MHz / 62.5 kHz BW / SF7 / CR5.
**Firmware version:** not captured — `self_info` does not carry a firmware version field.
**Script:** `diagnostics/radio/t9_liveness_latency.py`, command timeout 2.0s.
**Data:** `diagnostics/t9_run_20260419_234008.jsonl` (185 lines).

**Results:**

| Interval | n | ok | timeout | p50 ms | p95 ms | p99 ms | min ms | max ms |
|----------|----|----|---------|--------|--------|--------|--------|--------|
| 10s | 120 | 117 | 3 | 5.94 | 6.33 | 8.15 | 5.32 | 1106.9 |
| 30s | 40 | 40 | 0 | 5.95 | 6.22 | 6.32 | 5.36 | 6.32 |
| 60s | 20 | 18 | 2 | 5.67 | 24.39 | 106.14 | 5.24 | 126.58 |

**Key observations:**

1. Baseline latency is ~5.9 ms p50 across all three polling frequencies. The local USB-serial command path is healthy and fast.
2. Five timeouts (>2000 ms) in 180 total calls: 3 at the 10s interval, 0 at 30s, 2 at 60s. Timeouts occur even with no mesh_bot contention on the serial port.
3. The 10s interval's max of 1106.9 ms is a single sample within the 2s timeout but three orders of magnitude above p50. Occasional multi-second command latencies happen on this hardware.
4. Zero `silence_detected` events in the entire run (the script emits these at 3+ consecutive timeouts). All five timeouts were isolated — timeout gaps were 240s–540s apart.
5. `cpu_percent_delta` is null in all three summaries; psutil was not installed for this run. Load-average deltas are available: effectively zero at 30s and 60s intervals, slightly negative at 10s (−0.108 on 1-min average, meaning load was already declining). This is an instrumentation gap — future runs should install psutil.
6. `public_key` unchanged across the hour (drift dict empty in `self_info_end`). No SPIFFS identity drift on this run. One hour is a weak check — T6 needs sustained reset cycling to properly evaluate identity stability.
7. `get_stats_core` returns `payload_keys: ["battery_mv", "uptime_secs", "errors", "queue_len"]` on every successful call.

**Design implications for Phase 3:**

1. **Per-command timeout should be ≥5s, not 2s.** Max latency on a healthy radio was 1106.9 ms; meshcore_py's own `DEFAULT_TIMEOUT` is 5s. A 2s timeout generated 5 false timeouts in one hour of idle operation.
2. **Hang detection should require multiple consecutive timeouts, not a single one.** Five isolated timeouts occurred in healthy operation; zero consecutive triplets. A 3-consecutive-timeout rule fires zero times on this run.
3. **Polling interval of 30s is a good default.** At 30s: zero timeouts in 1200s, tight p99 (6.32 ms). 10s triples USB traffic with no detection benefit. 60s slows worst-case detection to 3×60s = 180s before the consecutive-timeout rule fires.
4. **Worst-case hang detection latency with 30s polling + 3-consecutive rule = ~90s.** This is the design target for Phase 3.

**Caveats:**

- Single run on a single device. Numbers are not a distribution.
- mesh_bot stopped during run — no TX contention on the serial channel. Real deployment has mesh_bot holding the port and issuing commands concurrently; T9 did not measure that scenario. Latency and timeout rates under load could be worse.
- n=20 at 60s is too few for reliable p99. The 106.14 ms figure is one sample of 20 being slow, not statistically meaningful.
- Firmware version not captured. Future runs should record it manually (log line from mesh_bot startup, or `git describe` in the firmware tree at flash time).

**Outcome (2026-04-21):** CIV-41 adopted all four recommendations: 30s poll interval, 5s per-ping timeout, 3-consecutive-timeout threshold (≈90s worst-case detection). The implementation also added an independent outbox-failure trigger (3 consecutive `send_chan_msg` failures) because the April 19 hang pattern was "alive but slow during TX" — liveness pings alone would have seen a healthy radio while sends were failing. See `recovery.py` and `docs/recovery.md`.

## Recovery characterization — April 20–21, 2026

**Setup:**

- Host: `civicmesh` (Raspberry Pi 4, dev bench)
- Radio: Heltec V3, firmware 1.15.0 (from OLED boot screen), `self_info.name = 6AA0BD72`
- meshcore_py 2.3.6
- Serial path: `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`
- mesh_bot stopped during runs (exclusive serial access)
- Polling interval 2s, command timeout 3s, hang threshold 10 consecutive timeouts
- Reset methods available: RTS pulse, pyusb `device.reset()`. sysfs authorize toggle deliberately excluded after it was observed to leave the device unreachable for 30 minutes during earlier bench work.
- Script: `diagnostics/radio/recovery_characterization.py`, `--mode run`
- Data: `diagnostics/radio/runs/recovery_20260420_215400.jsonl` (14,375 lines), `diagnostics/radio/runs/recovery_20260421_082715.jsonl` (14,304 lines)

**Runs:**

| Run | Duration | Probes | Timeouts | Slow probes (>100ms) | Hangs | p50 | p95 | p99 | max |
|-----|----------|--------|----------|----------------------|-------|-----|-----|-----|-----|
| Overnight (quiet) | 8h | 14,361 | 11 | 25 | 0 | 5.47ms | 5.60ms | 5.75ms | 2887.62ms |
| Daytime (busy) | 8h | 14,184 | 117 | 276 | 0 | 5.46ms | 5.70ms | 627.42ms | 2997.93ms |

**Slow probe distribution, daytime run** (276 probes with latency > 100ms):

| Bucket | Count |
|--------|-------|
| 100–250ms | 56 |
| 250–500ms | 54 |
| 500–1000ms | 72 |
| 1000–2000ms | 21 |
| 2000–3000ms | 73 |

**Sanity test recovery times** (from `recovery_20260420_214914.jsonl`, the successful sanity run immediately before the overnight run):

| Method | Duration |
|--------|----------|
| RTS pulse | 2669.7ms |
| pyusb `device.reset()` | 532.2ms |

Two earlier sanity runs failed: the first because pyusb lacked permissions (`[Errno 13] Access denied` — fixed by adding a udev rule for VID `10c4`), the second because `/dev/ttyUSB0` was renamed to `/dev/ttyUSB1` after re-enumeration (fixed by switching `config.toml` to the stable `/dev/serial/by-id/...` symlink).

**Key observations:**

1. **Baseline is extremely consistent.** p50 and p95 are nearly identical between quiet and busy runs (within 0.1ms). The radio responds in ~5.5ms when it responds promptly, regardless of mesh activity.

2. **Tail extends proportional to mesh activity.** p99 jumped from 5.75ms (quiet) to 627.42ms (busy). p99.9 from 530.06ms to 2807.83ms. Only the tail moves — the body of the distribution stays put. This is consistent with firmware being periodically busy servicing mesh traffic, not with steady-state degradation.

3. **Zero consecutive timeouts in either run.** All 128 timeouts across both runs had `consecutive_timeouts: 1`. Even in the busiest hour of the daytime run (21 timeouts in hour 2, ~1 every 3 min), none chained. The 10-consecutive-timeout hang detector never fired.

4. **The slow-probe distribution is bimodal.** Daytime run: 56+54+72 = 182 probes in the 100–1000ms range, but 73 probes in the 2000–3000ms range, with a dip (21 probes) at 1000–2000ms. Two distinct "busy" regimes — one that resolves in under a second, and one that butts up against the command timeout.

5. **Timeouts are isolated, not clustered into hangs.** Busy periods produced bursts of slow probes interspersed with healthy ones. No sustained silence, ever. Timeout hourly distribution in the daytime run: hours 0–7 had [2, 14, 21, 19, 10, 20, 16, 15] timeouts — spread across the entire run, not concentrated.

6. **pyusb reset was never exercised in production polling.** The sanity test confirmed it works (RTS recovered in ~2.7s, pyusb in ~0.5s), but production polling never invoked it because no hang was detected.

**Design implications for CIV-41:**

1. **Raise command timeout from 3s to 5s.** Of 117 daytime timeouts, most were near the 3s boundary (73 "slow successes" landed in 2000–3000ms). A 5s timeout would have converted most into successful slow probes, yielding a cleaner "timeout = actual hang signal" property. meshcore_py's own `DEFAULT_TIMEOUT` is 5s. Low-risk change.

2. **Keep 10-consecutive-timeout detector rule.** Zero false positives across 28,545 probes over 16 hours, including daytime traffic. The threshold is conservative and correct.

3. **Liveness-ping alone misses the April 19 failure class.** The `no_event_received` errors observed in mesh_bot logs on April 19 were almost certainly latency spikes during active TX, not silent hangs. A liveness ping probing between sends would have seen a healthy radio. CIV-41's secondary trigger on outbox send failures is therefore not optional — it's how you detect the dominant failure mode on this hardware.

4. **Consider dropping pyusb from the production recovery ladder.** It was built and verified, but never invoked across 16h of real polling. pyusb guards against CP2102 bridge hangs (failure mode D1 from `failure-modes.md`), which this hardware does not appear to exhibit at the rates that matter. RTS + the CIV-46 Arduino watchdog (hardware power cycle) may be sufficient. Keep pyusb as a path in CIV-41 but treat it as untested in production.

5. **The "alive but slow" failure mode is the dominant one, not silent hangs.** 276 slow probes vs 128 timeouts vs 0 hangs across 16 hours. CIV-41's job in production will mostly be riding out busy windows, not resetting dead radios. Design accordingly: outbox retries, patience, and liveness-ping confirmation before any escalation.

**Caveats:**

- mesh_bot stopped during both runs — no TX contention on the serial port. Real production has mesh_bot actively sending and receiving concurrently with the liveness ping. The interaction between TX and the slow-response tail is not characterized here.
- Single device under test. Two 8h samples on one Pi 4 + one Heltec V3 is a small N. Numbers are directional, not distributional.
- Passive RX only. Whatever is making the radio's firmware busy during daytime windows (RX processing? routing? adverts?) is inferred, not measured.
- Both runs were on a Pi 4. The deployment target is a Pi Zero 2W. Pi Zero 2W has a weaker CPU and a different USB architecture; recovery times and failure modes may differ.
- Firmware version captured from the OLED boot screen, not programmatically. `self_info` does not carry a firmware version field.
- No attempt was made to reproduce the April 19 `no_event_received` hang or the original 1.11.0 hang. Those were in-mesh_bot observations that the diagnostic harness cannot reproduce (exclusive-port constraint).

## Test-environment caveats

  - **Clock skew**: zero2w's clock runs ~12 seconds behind pi4 (no
    `chronyd` installed). Wall-clock latency math between the two
    nodes is meaningless until that's fixed; per-node `mono_ts` math
    is fine. Doesn't affect the heard-count feature design (which
    matches by content, not timing).
  - **Channel quietness varies by time of day**. T0 at 21:22 UTC saw
    rich `path_len` 3-11 traffic from KM7DKX, DuckArmy, etc.; T0 at
    23:21 UTC was nearly silent. If a T4 run shows no repeater
    activity, retry at a busier time before concluding repeaters are
    unreachable.
