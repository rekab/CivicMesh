# Repository guide

This file is the canonical project guide for both human contributors and
AI coding tools. Tools that auto-discover instructions at the repo root
— Claude Code, Codex, others — read this file as `AGENTS.md`. Human
contributors should read it too. The conventions and invariants below
are load-bearing for either audience.

## What this is

CivicMesh is a WiFi walk-up relay for MeshCore mesh channels at Seattle
Emergency Hubs. It runs a captive portal on a Raspberry Pi (dev: Pi 4,
prod: Pi Zero 2W) that lets users read and post to public MeshCore
radio channels during grid-down events. HTTP-only, offline-first, no
internet required.

## Project layout

Runtime modules at the repo root; vanilla-JS SPA in `static/` (no build
step); stdlib-`unittest` tests in `tests/`; design and operations docs
in `docs/`; bench tooling in `diagnostics/` (not installed as part of
the Python package).

## Commands

```bash
# Setup (uv manages venv + deps)
uv sync

# Run (dev)
uv run civicmesh-web --config config.toml     # captive portal
uv run civicmesh-mesh --config config.toml    # mesh radio relay
uv run civicmesh --config config.toml stats   # admin CLI

# Tests
uv run python -m unittest                                    # all tests
uv run python -m unittest tests.test_admin_outbox_list       # single test module
```

## Architecture

**Two-process model** sharing a single SQLite database (WAL mode):

- **web_server.py** — Synchronous HTTP server on port 8080. Serves the
  SPA, handles `/api/*` endpoints, captive-portal probes, session
  cookies, MAC-based abuse prevention, rate limiting. Queues outbound
  messages.
- **mesh_bot.py** — Async process using `meshcore`. Connects to the
  Heltec V3 radio over USB serial. Three concurrent async tasks:
  outbox sender (with exponential backoff), retention pruner, and
  heartbeat recorder.
- **database.py** — Schema and query functions. Tables: `messages`,
  `outbox`, `votes`, `sessions`, `status`, `heard_packets`. The DB is
  the sole IPC channel between the two processes; see
  `docs/message_lifecycle.md` for the outbox state machine and
  atomicity rules.
- **config.py** — Loads and validates `config.toml`.
- **civicmesh.py** — Operator CLI (SSH-only): pin/unpin, stats, outbox
  management, session queries.

## Invariants

These constraints must be preserved across all changes:

- UI must load even if the radio is disconnected; cached messages stay
  readable during outages.
- Outbound message state transitions are monotonic (queued ->
  sent/failed). Both the messages and outbox tables must be updated
  atomically in a single transaction — see `docs/message_lifecycle.md`.
- Rate limiting is enforced before enqueue. Input validation rejects
  empty/oversized messages.
- No HTTPS, accounts, or direct internet dependencies in v0.
- Portal hostnames must **not** use `.local` (iOS bypasses unicast DNS
  for `.local`; see `docs/ios-captive-portal-notes.md`).
- DB schema changes must preserve existing data or include migration.
- CPU/RAM usage must remain suitable for Pi Zero 2W.
- Security log must not include full message content.
- Provisioning must run `apt full-upgrade` and reboot before deployment
  (older kernels have a `brcmfmac` P2P crash from nearby iOS devices).

## Style and commits

- 4-space indentation, Python 3.13+ syntax. No linter or formatter
  configured; follow existing file style.
- Imperative commit summaries (e.g., "Add outbox retry logging"); PRs
  include a brief summary and manual-test notes. Include screenshots
  for `static/` changes.

## Configuration

Runtime config is in `config.toml`. When editing config values or
defaults in code, add a comment explaining why the change was made.
