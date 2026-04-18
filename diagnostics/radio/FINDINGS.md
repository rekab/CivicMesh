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

`mesh_bot.py:274-277` derives the shared channel secret as
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
