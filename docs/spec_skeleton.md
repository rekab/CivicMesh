# CivicMesh v0 Spec Skeleton

## Overview
- Purpose: offline, walk-up captive portal to read and post to a curated list of public MeshCore channels during grid-down events.
- Scope v0: HTTP-only UI, no accounts, no admin UI; read and post only to public channels; MeshCore radio over USB serial.
- Constraints: existing lightweight web server and SQLite are the base; no heavy frameworks; offline-first.

## Functional Requirements

### MUST
- Serve a captive portal that loads even if the radio is disconnected.
- Show cached messages for configured public channels.
- Allow posting to those channels and enqueue outbound messages.
- Display message state at minimum: queued, sent-to-radio, failed/retrying.
- Enforce abuse-resistance controls before enqueue (cookies/local storage, per-session rate limits, IP/MAC when available).
- Persist inbound and outbound messages to SQLite as the source of truth.
- Degrade gracefully with clear status when the radio is unavailable.

### SHOULD
- Surface repeat/ack/receipt indicators if MeshCore reliably provides them.
- Provide lightweight node health indicators in the UI (radio connected, queue length, last seen).
- Provide minimal operator recovery controls via CLI (e.g., clear stuck queue).

### COULD
- Add optional lightweight browser fingerprint signal to rate limiting (privacy-preserving).
- Add a local status page for operators (no auth; local only).

## Non-Functional Requirements

### Reliability
- UI renders even if the radio is absent or meshcore_py errors.
- Outbound queue survives restarts and power loss.
- Message state transitions are consistent and monotonic.

### Power
- Favor batch sends and exponential backoff to reduce radio and CPU usage.
- Avoid tight polling loops; keep disk writes bounded.

### Abuse Resistance
- Default-on posting rate limits with layered signals.
- Controls protect mesh throughput without building durable identities.

## Data Model (SQLite, WAL mode)

See `docs/message_lifecycle.md` for the message/outbox state machine and atomicity constraints.

### Tables

**messages** — all community-visible posts (inbound mesh, local, and wifi-originated).

| column | type | notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY | |
| ts | INTEGER NOT NULL | unix epoch seconds |
| channel | TEXT NOT NULL | channel name, e.g. "#civicmesh" |
| sender | TEXT NOT NULL | display name |
| content | TEXT NOT NULL | message body |
| source | TEXT NOT NULL | "mesh", "wifi", or "local" |
| session_id | TEXT | poster's session (wifi/local only) |
| fingerprint | TEXT | browser fingerprint hash |
| upvotes | INTEGER DEFAULT 0 | |
| downvotes | INTEGER DEFAULT 0 | |
| pinned | INTEGER DEFAULT 0 | |
| pin_order | INTEGER | |
| outbox_id | INTEGER | FK to outbox.id (wifi only) |
| status | TEXT | "queued"/"sent"/"failed" for wifi; NULL for mesh/local/legacy |

**outbox** — send queue for wifi-originated messages. Consumed by mesh_bot.

| column | type | notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY | |
| ts | INTEGER NOT NULL | original post time |
| channel | TEXT NOT NULL | |
| sender | TEXT NOT NULL | |
| content | TEXT NOT NULL | |
| session_id | TEXT NOT NULL | |
| fingerprint | TEXT | |
| sent | INTEGER DEFAULT 0 | legacy flag, 1 = sent |
| retry_count | INTEGER NOT NULL DEFAULT 0 | attempts so far |
| status | TEXT NOT NULL DEFAULT 'queued' | "queued"/"sent"/"failed" |
| heard_count | INTEGER NOT NULL DEFAULT 0 | echo count from mesh |
| min_path_len | INTEGER | shortest echo path |
| first_heard_ts | INTEGER | first echo timestamp |
| last_heard_ts | INTEGER | most recent echo |
| best_snr | REAL | best SNR across echoes |
| sender_ts | INTEGER | wall-clock second of first send attempt |

**votes** — per-session upvotes/downvotes on messages.

| column | type | notes |
|--------|------|-------|
| message_id | INTEGER NOT NULL | PK with session_id |
| session_id | TEXT NOT NULL | PK with message_id |
| vote_type | INTEGER NOT NULL | 1=upvote, -1=downvote |
| ts | INTEGER NOT NULL | |

**sessions** — walk-up user sessions (cookie-based, no accounts).

| column | type | notes |
|--------|------|-------|
| session_id | TEXT PRIMARY KEY | |
| name | TEXT | display name |
| location | TEXT | stamped with `cfg.node.site_name` on session create/update (CIV-11; column name kept for back-compat) |
| mac_address | TEXT | for abuse prevention |
| fingerprint | TEXT | browser fingerprint |
| created_ts | INTEGER | |
| last_post_ts | INTEGER | for rate limit window |
| last_seen_ts | INTEGER | for active-user counting |
| post_count_hour | INTEGER DEFAULT 0 | rolling rate limit counter |

**status** — process heartbeats.

| column | type | notes |
|--------|------|-------|
| process | TEXT PRIMARY KEY | "mesh_bot" |
| last_seen_ts | INTEGER NOT NULL | |
| radio_connected | INTEGER NOT NULL DEFAULT 0 | |

**heard_packets** — raw radio packet log for `/api/stats` histograms.

| column | type | notes |
|--------|------|-------|
| ts | INTEGER NOT NULL | |
| payload_type | INTEGER NOT NULL | |
| route_type | INTEGER NOT NULL | |
| path_len | INTEGER NOT NULL | |
| last_path_byte | INTEGER | last hop identifier |
| snr | REAL | |
| rssi | INTEGER | |

### Indexes
- messages(channel, ts DESC) — channel feeds
- messages(pinned, pin_order) — pinned message ordering
- outbox(sent, ts) — legacy pending lookup
- outbox(status, ts) — pending send queue
- votes(message_id) — vote lookups
- sessions(mac_address) — session-by-MAC lookups
- heard_packets(ts) — stats time-range queries

### Retention and Pruning
- `cleanup_retention_bytes_per_channel`: deletes oldest unpinned messages when channel exceeds byte budget. Protects queued messages (`status != 'queued'`).
- `prune_heard_packets`: deletes heard_packets older than 8 days. Runs hourly in mesh_bot's retention task.
- `prune_terminal_outbox`: deletes sent/failed outbox rows older than 8 days. The messages rows retain their status independently; the LEFT JOIN returns NULL for pruned outbox columns.

## UI Flows

### Read Channels
- Portal loads -> select channel -> view latest messages from cache.
- Indicate radio offline and cache staleness if applicable.

### Post Message
- Submit -> validate length and rate limit -> enqueue.
- Message appears immediately in timeline as "Queued for mesh".

### View Status
- Per-message indicator: queued, sent-to-radio, failed.
- Failed messages show retry count on tap.
- Sent wifi messages show heard count (echo repeats) on tap.

## Outbound Pipeline

See `docs/message_lifecycle.md` for the full state machine.

### Summary
- `/api/post` atomically creates both an outbox row and a messages row (`status='queued'`).
- mesh_bot's `_outbox_task` polls `outbox WHERE status='queued'`, sends via radio, and atomically updates both tables on success or failure.
- Echo-aware retry: after `no_event_received`, waits for mesh echoes before retrying. Pre-retry echo check on each subsequent attempt.
- Backoff: `[0, 2, 5, max_delay_sec]` sequence, reset after idle period. 3 consecutive send failures trigger `RecoveryController` (see `docs/recovery.md`).
- Max retries: 3 (configurable via `outbox_max_retries`).

## MeshCore Integration Assumptions
- Subscriptions are limited to configured public channels only.
- Receive: meshcore_py delivers inbound messages with channel id and timestamp; store immediately in messages.
- Send: meshcore_py provides synchronous success/failure for "sent to radio" only (not delivery).
- Echo detection via RX_LOG_DATA events provides best-effort delivery evidence (heard_count).

## Threat Model and Mitigations
- Threats: spam flooding, offensive content, radio DoS, local network probing.
- Mitigations:
  - Rate limits and per-signal throttling; conservative defaults.
  - Input validation (length; no HTML rendering).
  - Read-only curated channels list.
  - Security log records abuse and send failures without PII.
  - Degraded-mode messaging to reduce confusion and abuse.

## Deployment and Operations
- Systemd units: civicmesh-web starts after network; civicmesh-mesh depends on serial device.
- Startup order: DB check/migration -> reconciliation -> web server / mesh bot.
- Logging: separate app logs and security log; rotate with size limits.
- Upgrades: offline-safe update process; no internet dependency.
- Recovery: DB integrity check on boot; startup reconciliation fixes inconsistent message/outbox status; safe restart.

## Future Work (Placeholder)
- Admin/control via encrypted channels (no design in v0).
