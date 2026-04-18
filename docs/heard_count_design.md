# Heard-Count Feature — Design

Tracks how many times each **locally-originated** outbound channel
message is received back from the mesh (direct + via any repeater
hops). Surfaces a "heard via N" indicator in the CivicMesh web UI for
own messages. Does not apply to incoming traffic from other operators.

Ground for the design is in `diagnostics/radio/FINDINGS.md` —
particularly the firmware-dedup finding (CHANNEL_MSG_RECV is one
decode per packet) and the matching-window data from T4.

## Pleasant surprise: meshcore_py decrypts for us

`meshcore_py` 2.3.6 includes built-in channel decryption that fills
`message` / `sender_timestamp` / `txt_type` / `chan_name` directly into
the `RX_LOG_DATA.payload` dict. We don't need to do AES ourselves.
Enabled with `mc.set_decrypt_channel_logs(True)` after the connection
is established and channels are registered.

(`meshcore_parser.py` lines 90-148 in 2.3.6 — channel hash check via
HMAC, AES-ECB decrypt, sender_timestamp/txt_type/message extracted
from plaintext bytes. Cached by `pkt_hash` to avoid re-decrypting the
same packet across multiple physical receptions.)

This drops the implementation complexity meaningfully. We get to write
business logic, not crypto.

## Database changes

Add to `outbox` table (migration in the existing migration block in
`database.py`):

| column            | type         | notes                                       |
|-------------------|--------------|---------------------------------------------|
| `heard_count`     | INTEGER NOT NULL DEFAULT 0 | incremented on each RX_LOG_DATA match |
| `min_path_len`    | INTEGER NULL | smallest path_len seen (0 = direct)         |
| `first_heard_ts`  | INTEGER NULL | unix epoch s of first matched echo          |
| `last_heard_ts`   | INTEGER NULL | unix epoch s of most recent match           |
| `best_snr`        | REAL NULL    | highest SNR seen across echoes              |
| `sender_ts`       | INTEGER NULL | the wall-clock second we sent at; used as the matching key tie-breaker |

`sender_ts` is set when the row transitions to `sent` (it's `int(time.time())` at the moment we call `mc.commands.send_chan_msg`); the firmware uses that value as the on-the-wire `sender_timestamp` and it's what we'll see in incoming `RX_LOG_DATA.payload.sender_timestamp` echoes.

New helpers in `database.py`:

- `mark_outbox_sent(cfg, *, outbox_id, sender_ts, log)` — sets `status='sent'`, `sent=1`, `sender_ts=...`.
- `increment_heard(cfg, *, outbox_id, path_len, snr, ts, log)` — increments `heard_count`, updates min/last/best, sets `first_heard_ts` if NULL.

## In-memory active-outbox index

Lives inside the running `mesh_bot` process. Tracks recently-sent
outbox rows whose echoes we should still credit. Eviction by lifetime
(20s default, configurable).

Key shape: `(channel, content_text, sender_ts) → outbox_id`.

The `sender_ts` in the key is the tie-breaker for the
"undercount-over-false-positive" preference. If two outbox rows have
identical `(channel, text)` within the lifetime window — say two `ack`
sends 5 seconds apart — they have distinct `sender_ts` values (assuming
they're sent at least 1 second apart). On match we look up by full
triple; the second send's echoes never bleed into the first send's
heard-count.

If two sends genuinely happen in the same wall-clock second AND have
identical text (extremely rare and a sign of misuse), echoes are
attributed to whichever index entry the lookup hits first. Acceptable;
documented as a known edge case.

```python
class ActiveOutboxIndex:
    def __init__(self, lifetime_s: float = 20.0):
        self._entries: dict[tuple[str, str, int], tuple[int, float]] = {}
        # (channel, text, sender_ts) -> (outbox_id, expiry_mono)
        self._lifetime_s = lifetime_s

    def add(self, *, outbox_id: int, channel: str, text: str, sender_ts: int) -> None:
        key = (channel, text, sender_ts)
        self._entries[key] = (outbox_id, time.monotonic() + self._lifetime_s)

    def match(self, *, channel: str, text: str, sender_ts: int) -> int | None:
        # Exact triple match, plus expiry check.
        for ts in (sender_ts, sender_ts - 1, sender_ts + 1):
            entry = self._entries.get((channel, text, ts))
            if entry is None:
                continue
            outbox_id, expiry = entry
            if time.monotonic() > expiry:
                continue
            return outbox_id
        return None

    def evict_expired(self) -> None:
        now = time.monotonic()
        stale = [k for k, (_, exp) in self._entries.items() if now > exp]
        for k in stale:
            del self._entries[k]
```

Why ±1s sender_ts tolerance: the firmware-stamped `sender_timestamp`
can land in the second after we recorded our "send moment" if the
firmware processed our enqueue right at a second boundary. Tighter
than T4's ±2s because the active index is keyed on `(channel, text,
sender_ts)` — text already disambiguates most collisions.

## mesh_bot.py changes

After the existing `start_auto_message_fetching()` call and channel
setup loop, add:

```python
mesh_client.set_decrypt_channel_logs(True)
log.info("mesh:decrypt_channel_logs_enabled")

active_outbox = ActiveOutboxIndex(lifetime_s=20.0)

def _on_rx_log_data(event):
    try:
        p = event.payload
        if p.get("payload_typename") != "GRP_TXT":
            return
        msg = p.get("message")  # decrypted plaintext, "Name: Text"
        if not isinstance(msg, str):
            return
        chan_name = p.get("chan_name")
        if not chan_name:
            return
        # Strip our own "Name: " prefix to recover the original send text.
        prefix = f"{my_name}: "
        if not msg.startswith(prefix):
            return  # not our send
        text_only = msg[len(prefix):]
        sender_ts = p.get("sender_timestamp")
        if not isinstance(sender_ts, int):
            return
        outbox_id = active_outbox.match(
            channel=chan_name, text=text_only, sender_ts=sender_ts,
        )
        if outbox_id is None:
            return  # not in active window or not ours
        increment_heard(
            db_cfg,
            outbox_id=outbox_id,
            path_len=p.get("path_len"),
            snr=p.get("snr"),
            ts=int(time.time()),
            log=log,
        )
    except Exception as e:
        log.error("mesh:rx_log_data_error %s", e, exc_info=True)

mesh_client.subscribe(EventType.RX_LOG_DATA, _on_rx_log_data)
log.info("mesh:rx_log_data_subscribed")
```

`my_name` comes from `mesh_client.self_info["name"]` (already populated
by `send_appstart` inside `create_serial`).

In the existing send flow (`mesh_bot.py:107`), wrap the successful
send to register with the active index:

```python
sender_ts = int(time.time())
result = await mesh_client.commands.send_chan_msg(channel_idx, outbound)
if result.type != EventType.ERROR:
    mark_outbox_sent(db_cfg, outbox_id=int(item["id"]), sender_ts=sender_ts, log=log)
    active_outbox.add(
        outbox_id=int(item["id"]),
        channel=channel,
        text=outbound,
        sender_ts=sender_ts,
    )
```

Periodically call `active_outbox.evict_expired()` — either on a timer
or piggybacked on the existing outbox-poll loop.

## Out of scope for this iteration

- **Web UI rendering**. `heard_count` and `min_path_len` will be
  written to SQLite; surfacing them in the UI is a separate change.
  Confirm via `sqlite3 db.sqlite "SELECT id, content, heard_count,
  min_path_len FROM outbox WHERE sent=1 ORDER BY ts DESC LIMIT 10"`.
- **Heard-count for incoming traffic**. Per project memory, the
  feature scope is locally-originated only. Incoming messages will
  continue to use the existing `_on_channel_message` path with no
  changes.
- **Persisted active-outbox index**. The index is in-memory only; if
  mesh_bot restarts mid-window, late echoes from before the restart
  are dropped (one in-memory eviction's worth of undercount). Per the
  feedback memory, undercount > false-positive — so this is the
  preferred trade-off vs. complicating the schema with a persisted
  pending-match table.

## Verification plan

1. **Decryption smoke** before any production code change: temporarily
   add `mc.set_decrypt_channel_logs(True)` to
   `harness/node_side.py` and re-run T4. Confirm `RX_LOG_DATA.payload`
   gains `message` / `sender_timestamp` / `chan_name` fields. Revert
   the harness change after.
2. **Schema migration**: stop mesh_bot on a node, run mesh_bot once
   to trigger the migration, check `sqlite3 db.sqlite ".schema
   outbox"` shows the new columns.
3. **End-to-end on pi4 → zero2w**: send a wifi outbox message from
   pi4 (or use mesh_bot's test path), wait 20s, query `SELECT id,
   content, heard_count, min_path_len, last_heard_ts FROM outbox
   WHERE id=<id>` on pi4. Should show `heard_count >= 1` if a local
   repeater bounced our packet back. May be 0 if no repeater is in
   range — same caveat as T4.
4. **Negative**: send the same text twice, 30s apart. Confirm each
   send's heard_count tracks independently (the second send's echoes
   don't bleed into the first send's row).

## Open questions before implementation

1. Where should `ActiveOutboxIndex` live? Options:
   - inside mesh_bot.py as a local variable in the connect loop
   - a new module like `outbox_echoes.py` for testability
   - bolted onto the existing `database.py` as a class
   I'd lean toward a new small module, but happy to inline if you
   prefer.

2. Eviction timing: piggyback on the existing outbox-poll loop (which
   runs every ~Ns), or use a separate asyncio.create_task with its
   own interval? The piggyback is simpler; the dedicated task is more
   reliable if the poll loop ever stalls.

3. Anything else worth surfacing in the schema while we're at it?
   I'd lean toward keeping it minimal (the 6 columns above) and adding
   more later if the UI grows. But I notice the existing outbox table
   doesn't have a `sent_ts` separate from `ts` (which is queue time).
   Should we add `sent_ts INTEGER NULL` while we're in there, or is
   that scope creep?
