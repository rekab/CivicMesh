# Message Lifecycle

How messages flow through the system, from user post to mesh
transmission. Covers the two-table design, state machine, atomicity
requirements, and startup reconciliation.

## Two-table design: messages + outbox

WiFi-originated messages live in **two tables** simultaneously:

- **messages** — the canonical timeline. Every message visible in the UI
  is a row here, regardless of source (mesh, wifi, local). The `status`
  column tracks send state for wifi messages.
- **outbox** — the send queue. Only wifi messages that need to be
  transmitted over the mesh radio have rows here. mesh_bot polls this
  table and consumes items.

The link is `messages.outbox_id -> outbox.id`. The `get_messages` query
LEFT JOINs outbox to surface `heard_count` and `retry_count` alongside
message data.

### Why two tables instead of one?

The outbox carries send-specific metadata (retry_count, heard_count,
sender_ts, echo tracking) that doesn't belong on inbound mesh messages
or local-only messages. Keeping it separate avoids nullable column bloat
on every row in the main timeline.

## State machine

```
                        wifi post
                            |
                            v
                    +---------------+
                    |    queued     |  messages.status = 'queued'
                    |               |  outbox.status  = 'queued'
                    +-------+-------+
                            |
                  mesh_bot picks up
                            |
                +-----------+-----------+
                |                       |
           send succeeds           send fails
           (or echo confirms)      (no_event_received,
                |                   exception, etc.)
                v                       |
        +---------------+         retry_count++
        |     sent      |              |
        |               |     retry_count < max?
        +---------------+         |           |
        messages = 'sent'        yes          no
        outbox   = 'sent'        |            |
                            back to       +--------+
                            queued        | failed |
                                          +--------+
                                     messages = 'failed'
                                     outbox   = 'failed'
```

### State values

| messages.status | outbox.status | meaning |
|-----------------|---------------|---------|
| `'queued'` | `'queued'` | waiting for mesh_bot to send |
| `'sent'` | `'sent'` | transmitted over radio (may have echo confirmation) |
| `'failed'` | `'failed'` | retries exhausted, will not be retried |
| `NULL` | n/a | legacy or non-wifi message (mesh inbound, local) |

### Transitions

| from | to | triggered by | function |
|------|----|-------------|----------|
| queued | sent | send_chan_msg succeeds | `record_outbox_send()` |
| queued | sent | echo heard (heard_count > 0) during retry | `record_outbox_send()` |
| queued | failed | retry_count >= max_retries | `mark_outbox_failed()` |
| queued | failed | unknown channel (not in config) | `mark_outbox_failed()` |

All transitions update **both tables atomically** (see below).

## Atomicity constraints

The messages and outbox tables must stay in sync. There are two
critical points where this matters:

### 1. Creation: `/api/post`

When a user posts to a mesh channel, web_server creates both the outbox
row and the messages row. These must be in a **single transaction**
(`queue_outbox_and_message` in database.py) so that mesh_bot cannot see
the outbox row before the messages row exists.

**What goes wrong without this:** mesh_bot picks up the outbox row
between the two autocommit INSERTs, sends successfully, calls
`record_outbox_send` which does `UPDATE messages ... WHERE outbox_id=?`
-- but the messages row doesn't exist yet. The UPDATE matches 0 rows.
Later, web_server creates the messages row with `status='queued'`, and
it stays queued forever.

**Queue depth cap.** The same transaction also enforces
`limits.outbox_max_depth`: the function takes a required
`max_queue_depth` keyword and `SELECT COUNT(*) FROM outbox WHERE
status='queued'` runs before the INSERTs. If depth ≥ cap, the function
returns `None` and `/api/post` translates that to `429 {"error": "queue
full — try again in a few minutes", "retry_after_sec": 60}`. The user's
hourly quota is not consumed for refused posts (`record_post_for_session`
is skipped on the 429 path). This is the input gate for egress audit
F3 — it pairs with the `_outbox_task` token bucket (`limits.global_egress_per_hour`)
that gates output.

**Why `BEGIN IMMEDIATE` (not `BEGIN`).** `queue_outbox_and_message` opens
its transaction as `BEGIN IMMEDIATE` so the writer lock is acquired at
BEGIN time, before the `SELECT COUNT(*)`. With plain (deferred) `BEGIN`,
two `ThreadingHTTPServer` workers can both take read snapshots at
depth=N-1, both pass the cap check, and both INSERT — racing the cap by
N. `_retry_on_locked` covers `SQLITE_BUSY` contention but is not
load-bearing for cap correctness; `BEGIN IMMEDIATE` is.

### 2. Status transitions: send success/failure

When mesh_bot resolves a send (success or failure), it must update both
the outbox status and the messages status in a **single transaction**.
This is done inside `record_outbox_send()` and `mark_outbox_failed()` in
database.py, both of which use explicit `BEGIN`/`COMMIT`.

**What goes wrong without this:** if the process is killed between two
separate autocommit UPDATEs, the outbox can be marked 'sent' while the
messages row stays 'queued'. On restart, mesh_bot won't pick up the
outbox row (it's no longer 'queued'), and nothing ever updates the
messages row.

### Connection setup

All database functions use `isolation_level=None` (autocommit mode) via
`_connect()`. Explicit `BEGIN`/`COMMIT` blocks are used only where
multi-statement atomicity is required (the two cases above).

### Lock-contention resilience

Two processes (web_server, mesh_bot) share the database via WAL mode.
The `@_retry_on_locked` decorator in `database.py` retries on
`sqlite3.OperationalError("database is locked")` with sequential
backoff (50ms / 150ms / 450ms, 3 attempts).  Applied to the four
data-critical write functions where a lock failure would cause data
loss or duplicate sends:

- `insert_message` — inbound mesh message
- `queue_outbox_and_message` — user portal post
- `record_outbox_send` — marking an outbox row as sent
- `increment_heard` — echo count tracking

Not applied to best-effort writes (heartbeat, touch_session,
votes) where a missed write is harmless.

All mesh_bot DB calls run off the asyncio event loop: sync callbacks
use `_executor_db` (fire-and-forget with error logging via
`add_done_callback`), and `_outbox_task` uses `asyncio.to_thread`.
The decorator's `time.sleep` runs in the worker thread, not on the
event loop.

## Startup reconciliation

On startup, both mesh_bot and web_server call
`reconcile_message_status()`. This fixes any messages whose status is
out of sync with their outbox row:

- If outbox is 'sent' but messages is 'queued' -> set messages to 'sent'
- If outbox is 'failed' but messages is 'queued' -> set messages to 'failed'

This is a safety net for edge cases where the atomic transaction
couldn't prevent inconsistency (e.g., the write succeeded but the
process was killed before the transaction fully committed to WAL).

### Legacy migration caveat

`init_db()` contains a legacy migration that marks queued outbox rows as
'sent' if a matching messages row exists with `source='wifi'`. This was
written before Strategy A (inserting into messages at post time). The
migration now requires `AND m.status IS NULL` so it only matches
pre-Strategy-A rows. Without this guard, **every queued outbox row would
be marked sent on startup** because Strategy A messages always have a
matching `source='wifi'` row.

## Echo-aware retry

When `send_chan_msg` returns `no_event_received` (radio ack lost over
USB), mesh_bot waits `outbox_echo_wait_sec` (default 8s) for mesh
echoes before deciding to retry. If `heard_count > 0` after the wait,
the send is treated as successful.

Additionally, before each retry attempt (`retry_count > 0`), mesh_bot
re-checks `heard_count`. If an echo arrived between attempts, the retry
is skipped and the message is marked sent.

Echo matching uses `ActiveOutboxIndex` (in-memory, single-threaded on
the event loop). Entries expire after 20s. See
`docs/heard_count_design.md` for the matching semantics.

## Process responsibilities

### web_server.py

- **Creates** messages + outbox rows atomically on `/api/post`
- **Reads** messages (with outbox LEFT JOIN) on `/api/messages`
- **Never writes** to outbox status/sent columns

### mesh_bot.py

- **Reads** outbox rows via `get_pending_outbox(status='queued')`
- **Updates** both tables atomically on send success (`record_outbox_send`)
  or failure (`mark_outbox_failed`)
- **Creates** messages rows for inbound mesh traffic (`source='mesh'`, no outbox row)
- **Runs** startup reconciliation, retention pruning, and heartbeat

### civicmesh.py

- **Reads** both tables for display
- **Can cancel** queued outbox items (`cancel_outbox_message`)
- **Can reset** session rate limits (`sessions reset`)
- **Never modifies** message status directly

## Retention

| what | policy | function |
|------|--------|----------|
| messages | byte budget per channel, oldest unpinned first (protects queued) | `cleanup_retention_bytes_per_channel` |
| outbox (sent/failed) | 8 days | `prune_terminal_outbox` |
| heard_packets | 8 days | `prune_heard_packets` |

After outbox rows are pruned, the LEFT JOIN in `get_messages` returns
NULL for `retry_count` and `heard_count`. The frontend handles this
gracefully (omits the detail row when null).
