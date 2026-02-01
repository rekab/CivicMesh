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

- Spec skeleton: `docs/spec_skeleton.md`
- Invariants: `docs/invariants.md`
- Open questions: `docs/open_questions.md`
- Staged hardening plan: `docs/staged_plan.md`
- Captive portal setup: `docs/captive_portal_setup.md`

## Scope Notes (v0)

- Public channels only; no accounts and no web-based admin controls.
- HTTP-only captive portal for device compatibility.
- `sent-to-radio` indicates the message was handed to the radio, not delivered to recipients.
- Offline-first: UI loads without radio; cached messages remain readable.

## Configuration

Edit `config.toml`.

### Logging

Logs are written to `logs/` by default:
- `logs/web_server.log`
- `logs/mesh_bot.log`
- `logs/security.log` (ERROR+ security events, rate-limited to reduce log flooding)

## Initial Setup (dev or deployment)

Create and activate a local virtual environment (unprivileged), then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

## Deployment

1. **Copy files to Raspberry Pi** (via scp, rsync, or git clone)

2. **Create and activate a virtual environment (unprivileged):**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   ```

3. **Install Python dependencies:**
   ```bash
   pip install .
   ```

4. **Edit configuration:**
   ```bash
   nano config.toml
   ```
   - Set `serial_port` to match your USB device (usually `/dev/ttyUSB0`)
   - Configure WiFi SSID
   - Set hub name and location
   - Configure channels to join

5. **Install systemd services:**
   ```bash
   sudo cp systemd/*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable mesh-bot.service web-server.service
   ```

6. **Start services:**
   ```bash
   sudo systemctl start mesh-bot.service web-server.service
   ```

7. **Check status:**
   ```bash
   sudo systemctl status mesh-bot.service
   sudo systemctl status web-server.service
   ```

8. **View logs:**
   ```bash
   tail -f logs/mesh_bot.log
   tail -f logs/web_server.log
   tail -f logs/security.log
   ```

When deployed without internet access, administration and updates are performed over SSH on the WiFi AP using `apt-offline`.

## Run (dev)

In two terminals:

```bash
source .venv/bin/activate
python3 web_server.py --config config.toml
```

```bash
source .venv/bin/activate
python3 mesh_bot.py --config config.toml
```

Then browse to `http://<pi-ip>/`.

## Admin CLI (SSH only)

```bash
python3 admin.py --config config.toml pin 123
python3 admin.py --config config.toml unpin 123
python3 admin.py --config config.toml stats
python3 admin.py --config config.toml cleanup
python3 admin.py --config config.toml messages recent --channel "#fremont" --source wifi --limit 20
```

## Tests

Run unit tests with:

```bash
python3 -m unittest
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
