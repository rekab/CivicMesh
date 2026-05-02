# Civic Mesh Hub Relay Bot

WiFi walk-up relay for MeshCore mesh channels at Seattle Emergency Hubs.

## Hardware Requirements

### Development
- **Raspberry Pi 4** (4GB RAM recommended)
- **Heltec V3** running MeshCore companion firmware
- USB-A to USB-C cable (for Heltec V3 connection)

### Production Deployment
- **Raspberry Pi Zero 2W** (lower power, sufficient for deployment)
- **Heltec V3** running MeshCore companion firmware
- USB-A to USB-C cable (for Heltec V3 connection)

## Hardware Setup

### 1. Connect Heltec V3 to Raspberry Pi

1. **Power off the Raspberry Pi** (recommended for safe USB connection)
2. Connect the Heltec V3 to the Raspberry Pi via USB:
   - Plug USB-C end into the Heltec V3
   - Plug USB-A end into any USB port on the Raspberry Pi
3. Power on the Raspberry Pi

### 2. Verify USB Connection

After boot, check that the device is recognized:

```bash
ls -l /dev/ttyUSB*
```

You should see `/dev/ttyUSB0` (or `/dev/ttyUSB1`, etc. if other USB serial devices are connected).

If the device doesn't appear:
- Check USB cable connection
- Try a different USB port
- Check `dmesg | tail` for USB device detection messages
- Ensure Heltec V3 is powered (may need external power if Pi USB port doesn't provide enough)

### 3. Set USB Permissions (if needed)

If you get permission denied errors, add your user to the `dialout` group:

```bash
sudo usermod -a -G dialout $USER
```

Log out and back in for the change to take effect.

### 4. Configure WiFi Access Point

The Raspberry Pi needs to be configured as a WiFi access point. This is typically done via:
- `hostapd` for the access point
- `dnsmasq` for DHCP/DNS (optional, for captive portal)

The SSID is configured in `config.toml` (see Configuration section below).

## Overview

Two processes share state via SQLite:
- `mesh_bot.py` (async): connects to Heltec via USB serial (MeshCore companion firmware), joins channels, logs messages, relays queued WiFi posts, handles DM searches
- `web_server.py` (sync): captive portal HTTP server (no HTTPS), interactive web UI, queues posts, votes, and session state

## Project Docs

Specs and planning:
- Spec skeleton: `docs/spec_skeleton.md`
- Invariants: `docs/invariants.md`
- Open questions: `docs/open_questions.md`
- Staged hardening plan: `docs/staged_plan.md`

Deployment and operations:
- Captive portal setup: `docs/captive_portal_setup.md`
- iOS captive portal notes: `docs/ios-captive-portal-notes.md`
- Power budget: `docs/power-budget.md`
- Telemetry: `docs/telemetry.md`

Feature designs:
- Message lifecycle (outbox state machine): `docs/message_lifecycle.md`
- Heard-count / echo tracking: `docs/heard_count_design.md`

Radio / hardware:
- Recovery implementation (state machine, ladder, observability): `docs/recovery.md`
- Heltec V3 recovery hardware reference: `docs/heltec-recovery.md`
- Radio-debugging deep dive (failure modes, boot, reset domains, test plan): `docs/radio-debugging/` — start at its [README](docs/radio-debugging/README.md)

## Diagnostics

`diagnostics/` holds ad-hoc bench tooling, separate from the runtime code in the repo root. It is **not** installed as part of the Python package.

- `diagnostics/radio/` — Mac-side test harness that drives both CivicMesh nodes' radios over SSH via the `meshcore_py` library, bypassing `mesh_bot`. Used to isolate library/radio bugs from app-layer behavior. See `diagnostics/radio/README.md` and `diagnostics/radio/FINDINGS.md`.
- `diagnostics/loadgen.py`, `diagnostics/check_laodtest.sh` — load-test helpers used during power-budget work. See `docs/power-budget.md` for context.

## Recovery

mesh_bot includes a silent-hang detector that watches for radio unresponsiveness via a periodic `get_stats_core` ping (3 consecutive timeouts ≈ 90s) and sustained outbox send failures (3 consecutive `send_chan_msg` errors — echo-confirmed sends and successful sends both reset the counter, so transient errors don't trigger recovery). When either trigger fires, the `RecoveryController` resets the Heltec V3's ESP32 via an RTS pulse on the serial port, reconnects, and verifies before declaring healthy. If recovery fails, the process enters `NEEDS_HUMAN` state (visible via the `status` table's `state` column) and keeps retrying on exponential backoff capped at 1 hour — the process never exits. See `recovery.py` for the implementation and `docs/heltec-recovery.md` for the hardware context.

## Scope Notes (v0)

- Public channels only; no accounts and no web-based admin controls.
- HTTP-only captive portal for device compatibility.
- `sent-to-radio` indicates the message was handed to the radio, not delivered to recipients.
- Offline-first: UI loads without radio; cached messages remain readable.

## Deployment

This section walks through deploying CivicMesh to a fresh Raspberry
Pi. The flow is: flash → SSH in → bootstrap → configure → apply →
verify. Plan ahead for step 5 ("apply"): once it runs, the Pi takes
over its own WiFi radio and disappears from your home network. Either
SSH in over Ethernet for that step (Pi 4 has wired Ethernet) or
expect to reconnect over the new CivicMesh AP afterwards.

### 1. Flash and prep the SD card

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and
fill in the pre-boot config (the gear icon):

- **Hostname** (e.g. `civicmesh-fremont`)
- **SSH** with your public key
- **WiFi credentials for your home network** — you'll SSH in over
  WiFi during initial setup, *before* CivicMesh takes over the radio

The home WiFi creds matter for SSH-during-bootstrap. Once `civicmesh
apply` runs in step 5, the Pi runs its own AP and won't be on your
home network anymore.

### 2. First boot and SSH in

Boot the Pi, find it on your network (`ping civicmesh-fremont.local`
or check your router's client list), and SSH in:

```bash
ssh <user>@civicmesh-fremont.local
```

### 3. Run bootstrap

Two flavors. Inspect-then-run is recommended:

```bash
curl -LO https://raw.githubusercontent.com/rekab/CivicMesh/main/scripts/civicmesh-bootstrap.sh
less civicmesh-bootstrap.sh   # eyeball what it'll do
sudo bash civicmesh-bootstrap.sh
```

Or one-liner, if you've already vetted the script:

```bash
curl -sSL https://raw.githubusercontent.com/rekab/CivicMesh/main/scripts/civicmesh-bootstrap.sh | sudo bash
```

Bootstrap installs system packages, disables conflicting services,
sets up rfkill, creates the `civicmesh` user, installs uv, clones
the repo into `/usr/local/civicmesh/app/`, builds the prod venv, and
symlinks `civicmesh{,-web,-mesh}` into `/usr/local/bin/`. It stops
at "venv built" — it does not run `configure` or `apply`. Re-running
bootstrap on a configured Pi is safe.

### 4. Configure

```bash
sudo -u civicmesh civicmesh configure
```

Walks through prompts for hub name, location, channels, AP SSID,
etc. Writes `/usr/local/civicmesh/etc/config.toml`.

### 5. Apply

```bash
sudo civicmesh apply
```

Renders the system files (hostapd, dnsmasq, nftables, NetworkManager
unmanage config, systemd-networkd config, sysctl IPv6 disable, the
two CivicMesh systemd units) and starts the services.

**This is the step that takes over the WiFi radio.** Your SSH
session over WiFi will drop here. Run `apply` from a wired SSH
session (Pi 4) or be ready to reconnect over the new CivicMesh AP.

### 6. Verify

```bash
civicmesh stats
```

prints a counters line. The AP SSID configured in step 4 should
appear in WiFi scans, and walk-up users on that AP land on the
captive portal at `http://10.0.0.1/`.

### Updates after first deploy

Bootstrap is one-shot. To roll new code out to a Pi after the
initial deploy, use `civicmesh promote` from your dev checkout:

```bash
# On your dev machine, in your CivicMesh checkout on main:
uv run civicmesh promote --from .
```

`promote` ships your `main` branch to the Pi, rebuilds the prod
venv, and restarts services. It does not touch config or database.

## Run (dev)

First time? [Install uv](https://docs.astral.sh/uv/) and run `uv
sync` from the repo root to set up the venv.

In two terminals:

```bash
uv run civicmesh-web --config config.toml
```

```bash
uv run civicmesh-mesh --config config.toml
```

Then browse to `http://<pi-ip>/`.

## Admin CLI (SSH only)

```bash
uv run civicmesh --config config.toml pin 123
uv run civicmesh --config config.toml unpin 123
uv run civicmesh --config config.toml stats
uv run civicmesh --config config.toml cleanup
uv run civicmesh --config config.toml messages recent --channel "#fremont" --source wifi --limit 20
```

## Tests

Run unit tests with:

```bash
uv run python -m unittest
```

## Configuration

Edit `config.toml` before first run.

### Serial Port

Set `serial_port` to match your USB device:
- Usually `/dev/ttyUSB0` (first USB serial device)
- Check with `ls -l /dev/ttyUSB*` after connecting Heltec V3

### Channels

Configure which MeshCore channels the bot joins:
```toml
[channels]
names = ["#fremont", "#puget-sound"]
```

### Local Chatroom

Configure WiFi-only channels that never relay to the mesh:
```toml
[local]
names = ["#local"]
```

### Logging

Logs are written to `logs/` by default:
- `logs/web_server.log`
- `logs/mesh_bot.log`
- `logs/security.log` (ERROR+ security events, rate-limited to reduce log flooding)

## Security Notes

This system assumes a hostile environment:
- HTTP only (captive portal). Do not enter secrets.
- Posting and voting require a cookie + MAC validation (ARP lookup via `/proc/net/arp`).
- MAC/cookie mismatches are logged at high level to `logs/security.log`.
- Rate limiting prevents abuse (configurable `posts_per_hour`).
