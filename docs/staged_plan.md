# CivicMesh Staged Plan

## Stage 1: Verify and Stabilize Send/Receive
Acceptance criteria:
- Web UI loads with radio connected and disconnected.
- Outbound messages reach sent-to-radio within a defined target under normal conditions.
- Inbound messages are stored and visible in UI.
- No crashes during a 1-hour idle + 10-message burst test.

Acceptance-test checklist:
- WiFi AP up; portal reachable at `http://<pi-ip>/` and captive portal opens on iOS/Android.
- WiFi-only API responds: list channels, fetch recent messages, post a message, fetch status.
- Mesh radio unplugged: UI loads, shows degraded status, cached messages readable.
- Mesh radio plugged in: send a post, state transitions to sent-to-radio, message appears on peer radio.
- Restart mesh bot while web server running; UI remains responsive.

## Stage 2: Add Safety Rails
Acceptance criteria:
- Rate limiting blocks abuse without blocking normal usage.
- Backoff and retry logic prevents tight retry loops.
- UI clearly shows queued, sent-to-radio, failed/retrying.
- Security log records rate-limit hits and send failures.

Acceptance-test checklist:
- Rate limit triggers on rapid posting from same session; clear user-facing message shown.
- Rate limit respects new session/cookie while still throttling IP/MAC when available.
- Retry/backoff increases wait time after repeated failures; no tight loop in logs.
- Spam-style payloads (long, repeated, empty) are rejected and logged.
- Security log records rate-limit events without full message content.

## Stage 3: Harden for Field Deployment
Acceptance criteria:
- Clean recovery after power loss; queue preserved.
- Systemd services restart cleanly; no manual intervention required.
- Pruning rules keep DB size within configured limits.
- CPU/RAM use remains within Pi Zero 2W constraints over 24 hours.

Acceptance-test checklist:
- Power-cycle during active send: services recover, outbox not lost.
- DB integrity check passes on boot; corrupted rows are handled safely.
- Log rotation works; logs do not grow without bound.
- 24-hour soak: CPU/RAM stable, no runaway disk usage.

## Lightweight Field-Test Worksheet

### Setup
- Device: Pi model, SD card type, power source.
- Radio: Heltec V3 firmware version and serial port.
- WiFi: SSID, channel, DHCP/DNS setup.
- Time and location of test; ambient conditions.

### Captive Portal and WiFi API
- iOS: captive portal auto-opens (yes/no); portal loads; channel list visible.
- Android: captive portal auto-opens (yes/no); portal loads; channel list visible.
- Manual URL (`http://<pi-ip>/`) loads on both devices.
- API: fetch channels, fetch messages, post message, fetch status succeed.

### Messaging
- Post from phone A; appears as queued then sent-to-radio.
- Peer radio receives message; note time-to-receive.
- Radio unplugged: posting shows degraded state; cached messages readable.

### Rate Limiting and Spam Resistance
- Rapid post burst from same device triggers limit.
- New session/cookie still throttled by IP/MAC when available.
- Attempt long/empty/repeated messages; confirm rejection and log entry.

### Stability
- Power cycle during active traffic; recovery time.
- Mesh bot restart; UI still responsive.
- Note any crashes or UI failures.

### Notes and Measurements
- Queue length under load.
- Average send latency (queued -> sent-to-radio).
- Observed errors (with timestamps).
