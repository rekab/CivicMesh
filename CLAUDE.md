# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

CivicMesh is a WiFi walk-up relay for MeshCore mesh channels at Seattle Emergency Hubs. It runs a captive portal on a Raspberry Pi (dev: Pi 4, prod: Pi Zero 2W) that lets users read and post to public MeshCore radio channels during grid-down events. HTTP-only, offline-first, no internet required.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install .

# Run (dev)
python3 web_server.py --config config.toml    # captive portal
python3 mesh_bot.py --config config.toml      # mesh radio relay

# Run (installed)
civicmesh-web --config config.toml
civicmesh-mesh --config config.toml
civicmesh-admin --config config.toml stats

# Tests
python3 -m unittest                           # all tests
python3 -m unittest tests.test_admin_outbox_list  # single test module
```

No linter or formatter is configured. Follow existing style: 4-space indentation, Python 3.9+ syntax.

## Architecture

**Two-process model** sharing a single SQLite database (WAL mode):

- **web_server.py** — Synchronous HTTP server (`http.server`) on port 8080. Serves the captive portal SPA from `static/`, handles API endpoints (`/api/channels`, `/api/messages`, `/api/post`, `/api/vote`, `/api/session`, `/api/status`), captive portal detection probes, session management via cookies, MAC-based abuse prevention, and rate limiting. Queues outbound messages into the database.

- **mesh_bot.py** — Async process using `meshcore` library. Connects to Heltec V3 radio via USB serial, joins configured channels, stores inbound messages. Runs three concurrent async tasks: outbox sender (with exponential backoff), retention pruner, and heartbeat recorder.

- **database.py** — Schema definition and query functions. Tables: `messages`, `outbox`, `votes`, `sessions`, `status`, `heard_packets`. The database is the sole IPC mechanism between the two processes. See `docs/message_lifecycle.md` for the message/outbox state machine and atomicity constraints.

- **config.py** — Loads and validates `config.toml`. Hub settings, radio params, channel list, rate limits, retention policy.

- **admin.py** — Operator CLI (SSH-only): pin/unpin messages, stats, outbox management, session queries.

- **static/** — Vanilla JS single-page app (index.html, app.js, style.css). No build step.

## Invariants

These constraints must be preserved across all changes:

- UI must load even if the radio is disconnected; cached messages stay readable during outages.
- Outbound message state transitions are monotonic (queued -> sent/failed). Both the messages and outbox tables must be updated atomically in a single transaction — see `docs/message_lifecycle.md`.
- Rate limiting is enforced before enqueue. Input validation rejects empty/oversized messages.
- No HTTPS, accounts, or direct internet dependencies in v0.
- Portal hostnames must **not** use `.local` (iOS bypasses unicast DNS for `.local`; see `docs/ios-captive-portal-notes.md`).
- DB schema changes must preserve existing data or include migration.
- CPU/RAM usage must remain suitable for Pi Zero 2W.
- Security log must not include full message content.
- Provisioning must run `apt full-upgrade` and reboot before deployment (older kernels have a `brcmfmac` P2P crash from nearby iOS devices).

## Configuration

Runtime config is in `config.toml`. When editing config values or defaults in code, add a comment explaining why the change was made.
