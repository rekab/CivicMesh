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

## Data Model (SQLite)

### Tables (current schema)
- messages: id, ts, channel, sender, content, source ("mesh" | "wifi"), session_id, fingerprint, upvotes, downvotes, pinned, pin_order.
- outbox: id, ts, channel, sender, content, session_id, fingerprint, sent (0/1).
- votes: message_id, session_id, vote_type (1 or -1), ts.
- sessions: session_id, name, location, mac_address, fingerprint, created_ts, last_post_ts, post_count_hour.

### Indexes (current schema)
- messages(channel, ts DESC) for channel feeds.
- messages(pinned, pin_order) for pinned ordering.
- outbox(sent, ts) for pending sends.
- votes(message_id) for vote lookups.
- sessions(mac_address) for session association.

### Retention and Pruning
- Keep recent messages by age and/or count per channel (configurable).
- Periodic pruning of old sessions and vote rows.
- Avoid frequent vacuum/defrag to minimize writes.

## UI Flows

### Read Channels
- Portal loads -> select channel -> view latest messages from cache.
- Indicate radio offline and cache staleness if applicable.

### Post Message
- Submit -> validate length and rate limit -> enqueue.
- Confirm state as queued with best-effort language.

### View Status
- Per-message indicator: queued, sent-to-radio, failed/retrying.
- Optional retry count and last error in non-technical phrasing.

## Outbound Pipeline

### Queue -> Batching -> Send -> Retry/Backoff -> State Transitions
- Insert message into outbox with state queued and next_attempt_at=now.
- Worker selects due items, groups by channel, sends in small batches.
- On send attempt:
  - If serial/radio unavailable: mark failed/retrying, schedule backoff.
  - If send accepted by meshcore_py: mark sent-to-radio.
  - If send error: increment retry_count, update backoff, keep failed/retrying.
- Backoff: exponential with ceiling and jitter; reset on success.
- State transitions are monotonic and recorded.

## MeshCore Integration Assumptions
- Subscriptions are limited to configured public channels only.
- Receive: meshcore_py delivers inbound messages with channel id and timestamp; store immediately in messages.
- Send: meshcore_py provides synchronous success/failure for "sent to radio" only (not delivery).
- Any repeat/ack/receipt metadata is optional and treated as best-effort.

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
- Startup order: DB check/migration -> web server -> mesh bot.
- Logging: separate app logs and security log; rotate with size limits.
- Upgrades: offline-safe update process; no internet dependency.
- Recovery: DB integrity check on boot; auto-prune corrupted outbox rows; safe restart.

## Future Work (Placeholder)
- Admin/control via encrypted channels (no design in v0).
