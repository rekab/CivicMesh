# Deploying CivicMesh

This document walks through deploying CivicMesh from a fresh Raspberry
Pi and an unflashed Heltec V3. The flow is: flash radio → flash SD →
SSH in → bootstrap → configure → (optional) install docs → apply →
verify.

The cutover from your home network to the new CivicMesh AP happens
when you `sudo reboot` at the end of step 7 — `apply` itself stages
the change without touching the live radio, so your SSH session
survives it. Either SSH in over Ethernet for the deploy (Pi 4 has
wired Ethernet) or be ready to reconnect on the new SSID after the
reboot.

## 1. Flash the Heltec V3

Flash the MeshCore companion firmware to the Heltec V3 using the
official web flasher.

1. Plug the Heltec into your computer with a USB-C **data** cable.
2. Open <https://flasher.meshcore.co.uk> in Chrome or Edge (Web
   Serial isn't available in Firefox or Safari).
3. Select **Heltec V3** as the board, **Companion (USB)** as the
   firmware variant, and your region's frequency profile.
4. Click flash and follow the prompts. The flasher pops a serial
   port picker — pick the Heltec.

After flashing completes, press RST once. The OLED should show the
MeshCore boot screen.

Optional sanity check: open <https://config.meshcore.dev> in the same
browser, connect, and confirm you can talk to the firmware. You
don't need to configure anything here — `civicmesh configure` does
that on the Pi side later.

## 2. Flash and prep the SD card

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and
fill in the pre-boot config (the gear icon):

- **Hostname** (e.g. `civicmesh-test`)
- **SSH** enabled
- **WiFi credentials for your home network** — you'll SSH in over
  WiFi during initial setup, *before* CivicMesh takes over the
  radio

The home WiFi creds matter for SSH-during-bootstrap. Once you reboot
after step 7, the Pi runs its own AP and won't be on your home
network anymore.

## 3. First boot and SSH in

Boot the Pi, find it on your network (`ping civicmesh-test.local` or
check your router's client list), plug the flashed Heltec into a USB
port on the Pi, and SSH in:

```bash
ssh <user>@civicmesh-test.local
```

### Update and install optional packages

This may be the last time the node has access to the internet, so
now's the time to install updates:

```bash
sudo apt update && sudo apt upgrade
```

If you want bench-debugging utilities and convenience packages, now's
the time:

```bash
sudo apt install \
  apt-offline \
  dnsutils \
  fake-hwclock \
  git \
  htop \
  iotop \
  iw \
  jq \
  lsof \
  nmap \
  picocom \
  ripgrep \
  sqlite3 \
  tcpdump \
  tmux \
  tree \
  usbutils \
  vim
```

`fake-hwclock` matters specifically for the Pi Zero 2W — it has no
real-time clock, so timestamps would drift across reboots without
network time. The rest are bench conveniences.

## 4. Run bootstrap

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

## 5. Configure

```bash
sudo -u civicmesh civicmesh configure
```

Walks through prompts for hub name, location, channels, AP SSID,
etc. Writes `/usr/local/civicmesh/etc/config.toml`.

## 6. Install the hub reference library (optional)

While the Pi still has internet access, install the Seattle Hub
reference library so the Reference section appears in the channel
list:

```bash
curl -LO https://github.com/rekab/hub-docs-content/releases/download/v1/hub-docs-20260506T224246Z.zip
sudo -u civicmesh civicmesh install-hub-docs hub-docs-20260506T224246Z.zip
```

For newer releases, see
[`rekab/hub-docs-content` releases](https://github.com/rekab/hub-docs-content/releases).
Skip this step entirely for a messaging-only deployment.

## 7. Apply and reboot

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
comes back up on its own AP. Reconnect by joining the `CivicMesh-*`
SSID configured in step 5. (On a Pi 4 with wired Ethernet, only
WiFi drops; an SSH session over Ethernet survives the reboot.)

## 8. Verify

```bash
civicmesh stats
```

prints a counters line. The AP SSID configured in step 5 should
appear in WiFi scans, and walk-up users on that AP land on the
captive portal at `http://10.0.0.1/`. If you installed hub-docs in
step 6, you should also see the Reference section in the channel
list on the portal.

The UI should indicate that the radio is online, and you should be
able to send messages to public mesh channels.

If not, check `systemctl status civicmesh-mesh` and
`journalctl -u civicmesh-mesh`.

## Updates after first deploy

Bootstrap is one-shot. To roll new code out to a Pi after the
initial deploy, `git clone` the CivicMesh repo on your dev machine
and use `civicmesh promote`:

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
[promote / apply / reboot decision tree](civicmesh-tool.md#when-to-promote-apply-and-reboot)
for the rule.

## Getting the machine back on the internet

If you need to connect a node back to the internet (e.g. fetch
updates, sync the clock), the easiest thing to do is unplug the
Heltec and plug in a USB ethernet adapter.
