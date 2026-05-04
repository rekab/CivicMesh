# CivicMesh

**A walk-up MeshCore relay for Seattle Emergency Hubs.**

[Seattle Emergency Hubs](https://seattleemergencyhubs.org/) are neighborhood gathering points:
places people show up after a grid-down event to share
information. Mutual aid is the
infrastructure when the other infrastructure is overwhelmed.
CivicMesh runs at a Hub: a Raspberry Pi and a MeshCore
radio together become an offline WiFi access point. Anyone
in range opens the captive portal on their phone and reads
or posts to public mesh channels. No app. No account. No
uplink. The radio carries what people leave behind to
whoever is listening on the mesh.

At an active Hub, three volunteer roles exist solely to
move information: the Information Manager keeps the public
message boards current, the Radio Assistant triages
traffic, and the licensed Radio Operator passes
high-priority messages over the Seattle Auxiliary
Communications Service (ACS) net to the City and other
Hubs. That pipeline is finite. The net takes a few
messages per operator per sweep, all three roles are
task-saturated in any real event, and most
neighborhood-scale information will never make the
priority cut.

CivicMesh is a parallel channel for that overflow. A
neighbor with a phone walks up, posts a short note through
the captive portal, and it lands on the Hub's local feed
and on the LoRa mesh — without taking a slot on the ACS
net or a minute of the Radio Operator's time.

## Project status

CivicMesh is a working prototype, not a finished product.
Development consists of a few nodes run on the bench, but have
not been deployed in a real emergency or stress-tested by strangers at scale.

Near-term goals are field tests at hacker events, Seattle
Emergency Hub drills, and similar gatherings. It needs places
where the walk-up-WiFi model can be tried by people who
didn't build it. Feedback from those contexts is needed in this phase.

If you are a mesh radio operator, a Hub coordinator, or
someone who would benefit from this existing, the project
welcomes your input and your skepticism in roughly equal
measure.

## How it works

MeshCore is an open-source LoRa mesh of small, low-power
radios that relay messages neighbor-to-neighbor, no towers
and no internet required. By 2026 the regional MeshCore
network reaches from Vancouver BC to Portland: the same
corridor most exposed to a Cascadia subduction zone event.
CivicMesh plugs into that existing fabric rather than
building its own.

```
Phone browser
   ↕ WiFi, no internet
Raspberry Pi  (captive portal + SQLite)
   ↕ USB serial
Heltec V3  (MeshCore companion firmware)
   ↕ LoRa
MeshCore channels
```

Two processes share state through a single SQLite database
(WAL mode):

- `web_server.py` — synchronous HTTP server. Serves the
  captive portal SPA, handles posts and votes, manages
  sessions, enforces rate limits.
- `mesh_bot.py` — async process. Talks to the Heltec over
  USB serial via the `meshcore` library, joins channels,
  records inbound messages, drains the outbox onto the
  air.

Walk-up posts queue in SQLite and are paced onto the mesh.
Mesh messages land in the same database and become
readable in the portal. The radio link is best-effort;
nothing in the local read/post path depends on it.

## Light on the air

Mesh airtime is finite, and oversaturation during an event
is a real risk. CivicMesh adds traffic in two disciplined
steps:

- **At ingest:** each WiFi session is capped at 10 posts
  per hour and 100 characters per post. A burst from one
  phone can't dominate the outbox.
- **At egress:** the outbox is a serial queue — one
  message on the air at a time. The gap between
  consecutive sends ramps from 2 → 5 → 10 seconds under
  sustained load. After ~60 seconds of quiet, the ramp
  resets so an isolated post goes out immediately. Ten
  posts queued at once drain over about 80 seconds.

CivicMesh is **not a repeater**. It does not relay or
forward other nodes' traffic. Airtime consumed scales with
foot traffic at the Hub, not with mesh activity. All
limits are configurable; defaults are conservative on
purpose.

## Hardware

CivicMesh runs on a Raspberry Pi paired with a Heltec V3
LoRa board flashed with the MeshCore companion firmware,
connected by USB.

- **Dev:** Raspberry Pi 4 (4GB recommended)
- **Prod:** Raspberry Pi Zero 2W

Plug the Heltec into any USB port on the Pi (USB-A on the
Pi side, USB-C on the Heltec). That's the entire hardware
setup. The deployment scripts handle WiFi AP
configuration, package install, and the systemd unit's
serial-device access.

## Deployment

This section walks through deploying CivicMesh to a fresh Raspberry
Pi. The flow is: flash → SSH in → bootstrap → configure → apply →
verify. The cutover from your home network to the new CivicMesh AP
happens when you `sudo reboot` at the end of step 5 — `apply` itself
stages the change without touching the live radio, so your SSH
session survives it. Either SSH in over Ethernet for the deploy
(Pi 4 has wired Ethernet) or be ready to reconnect on the new SSID
after the reboot.

### 1. Flash and prep the SD card

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and
fill in the pre-boot config (the gear icon):

- **Hostname** (e.g. `civicmesh-fremont`)
- **SSH** with your public key
- **WiFi credentials for your home network** — you'll SSH in over
  WiFi during initial setup, *before* CivicMesh takes over the radio

The home WiFi creds matter for SSH-during-bootstrap. Once you reboot
after step 5, the Pi runs its own AP and won't be on your home
network anymore.

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

### 5. Apply and reboot

```bash
sudo civicmesh apply
sudo reboot
```

`apply` renders the system files (hostapd, dnsmasq, nftables,
NetworkManager unmanage config, systemd-networkd config, sysctl
IPv6 disable, the two CivicMesh systemd units), validates them,
writes them, and **stages** AP mode for the next boot. Your SSH
session survives `apply` — it doesn't touch the live radio.

The cutover happens when you `sudo reboot`. That's when hostapd
takes the radio, your home-network SSH session drops, and the Pi
comes back up on its own AP. Reconnect by joining the
`CivicMesh-*` SSID configured in step 4. (On a Pi 4 with wired
Ethernet, only WiFi drops; an SSH session over Ethernet survives
the reboot.)

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

Most code changes need only `promote`. Some changes also require
`sudo civicmesh apply` (rendered system files, systemd unit files,
config schema), and a few of those also require a reboot (changes
to hostapd, dnsmasq, nftables, networkd, NetworkManager, or sysctl
output). See the
[promote / apply / reboot decision tree](docs/civicmesh-tool.md#when-to-promote-apply-and-reboot)
for the rule.

## Development

First time? [Install uv](https://docs.astral.sh/uv/) and run
`uv sync` from the repo root.

Run the two services in separate terminals:

```bash
uv run civicmesh-web --config config.toml
uv run civicmesh-mesh --config config.toml
```

Then browse to `http://<pi-ip>:8080/`.

Run unit tests:

```bash
uv run python -m unittest
```

## Admin CLI (SSH only)

```bash
uv run civicmesh --config config.toml pin 123
uv run civicmesh --config config.toml unpin 123
uv run civicmesh --config config.toml stats
uv run civicmesh --config config.toml cleanup
uv run civicmesh --config config.toml messages recent --channel "#fremont" --source wifi --limit 20
```

On a deployed Pi the binary is on `PATH` and the config flag is
not needed (it defaults to `/usr/local/civicmesh/etc/config.toml`):

```bash
civicmesh stats
sudo civicmesh pin 123
```

## Configuration

The full schema and defaults live in
[`config.toml.example`](config.toml.example).

- **Dev:** copy `config.toml.example` to `config.toml` at the repo
  root and edit.
- **Prod:** `civicmesh configure` walks the common knobs
  interactively and writes `/usr/local/civicmesh/etc/config.toml`.
  To change something afterwards, edit that file directly; if the
  change touches a system-rendered setting (hostapd, dnsmasq,
  nftables, etc.) follow with `sudo civicmesh apply`. The
  [decision tree](docs/civicmesh-tool.md#when-to-promote-apply-and-reboot)
  spells out which changes also need a reboot.

A few common knobs:

```toml
[channels]
names = ["#fremont", "#puget-sound"]    # mesh channels to join

[local]
names = ["#local"]                      # WiFi-only, never relayed
```

## Security

This system assumes a hostile environment:

- HTTP only (captive portal). Do not enter secrets.
- Posting and voting require a cookie + MAC validation (ARP lookup
  via `/proc/net/arp`).
- MAC/cookie mismatches are logged at high level to the security
  log.
- Rate limiting prevents abuse (configurable `posts_per_hour`).

## Recovery

`mesh_bot` includes a silent-hang detector that watches for radio
unresponsiveness via a periodic `get_stats_core` ping (3 consecutive
timeouts ≈ 90s) and sustained outbox send failures (3 consecutive
`send_chan_msg` errors — echo-confirmed sends and successful sends
both reset the counter, so transient errors don't trigger
recovery). When either trigger fires, the `RecoveryController`
resets the Heltec V3's ESP32 via an RTS pulse on the serial port,
reconnects, and verifies before declaring healthy. If recovery
fails, the process enters `NEEDS_HUMAN` state (visible via the
`status` table's `state` column) and keeps retrying on exponential
backoff capped at 1 hour — the process never exits. See
`recovery.py` for the implementation and `docs/heltec-recovery.md`
for the hardware context.

## Scope (v0)

- Public channels only; no accounts and no web-based admin
  controls.
- HTTP-only captive portal for device compatibility.
- `sent-to-radio` indicates the message was handed to the radio,
  not delivered to recipients.
- Offline-first: UI loads without radio; cached messages remain
  readable.

## Project docs

Specs and planning:
- Spec skeleton: `docs/spec_skeleton.md`
- Invariants: `docs/invariants.md`
- Open questions: `docs/open_questions.md`
- Staged hardening plan: `docs/staged_plan.md`

Deployment and operations:
- Operator tool reference: `docs/civicmesh-tool.md`
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

`diagnostics/` holds ad-hoc bench tooling, separate from the
runtime code in the repo root. It is **not** installed as part of
the Python package.

- `diagnostics/radio/` — Mac-side test harness that drives both
  CivicMesh nodes' radios over SSH via the `meshcore_py` library,
  bypassing `mesh_bot`. Used to isolate library/radio bugs from
  app-layer behavior. See `diagnostics/radio/README.md` and
  `diagnostics/radio/FINDINGS.md`.
- `diagnostics/loadgen.py`, `diagnostics/check_laodtest.sh` —
  load-test helpers used during power-budget work. See
  `docs/power-budget.md` for context.
