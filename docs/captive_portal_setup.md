# Captive Portal Setup (Raspberry Pi OS, nftables)

This document defines the manual setup for a captive portal WiFi AP on Raspberry Pi OS.
It is written for offline deployment with no Ethernet uplink.

## Target OS
- Raspberry Pi OS (Dec 4 2025 release target; treat as a floating requirement).
- Use Lite image unless there is a specific UI need.

## Assumptions
- No Ethernet uplink during deployment.
- WiFi AP runs on `wlan0`.
- DHCP and DNS are served locally.
- HTTP-only portal for captive portal compatibility.

## Packages
Install the minimal packages:

```bash
sudo apt update
sudo apt install -y hostapd dnsmasq nftables
```

## hostapd
Create `/etc/hostapd/hostapd.conf`:

```conf
interface=wlan0
ssid=CivicMesh-Example
hw_mode=g
channel=6
wmm_enabled=0
auth_algs=1
ignore_broadcast_ssid=0
```

Enable hostapd to read the config in `/etc/default/hostapd`:

```conf
DAEMON_CONF="/etc/hostapd/hostapd.conf"
```

## dnsmasq
Create `/etc/dnsmasq.d/civicmesh.conf`:

```conf
interface=wlan0
bind-interfaces
port=53
no-resolv
bogus-priv
domain-needed

# DHCP range
server=1.1.1.1
address=/#/10.0.0.1

dhcp-range=10.0.0.50,10.0.0.150,255.255.255.0,12h

dhcp-option=option:router,10.0.0.1
dhcp-option=option:dns-server,10.0.0.1
```

Notes:
- `address=/#/10.0.0.1` forces DNS to the portal, suitable for offline use.
- If you prefer a different subnet, keep it consistent across all configs.

## Static IP for wlan0 (service-only)
A static IP is required for the AP interface to serve DHCP/DNS.
Use a local-only IP; no upstream route required.

Create `/etc/dhcpcd.conf` snippet:

```conf
interface wlan0
static ip_address=10.0.0.1/24
nohook wpa_supplicant
```

## nftables
Create `/etc/nftables.conf`:

```nft
#!/usr/sbin/nft -f

flush ruleset

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;

    ct state established,related accept
    iifname "lo" accept

    # DHCP
    udp dport 67 accept
    udp dport 68 accept

    # DNS
    udp dport 53 accept
    tcp dport 53 accept

    # HTTP portal
    tcp dport 80 accept

    # SSH for admin (optional, local only)
    tcp dport 22 accept
  }

  chain forward {
    type filter hook forward priority 0; policy drop;
  }

  chain output {
    type filter hook output priority 0; policy accept;
  }
}
```

Notes:
- No NAT rules are included because there is no uplink.
- If SSH should be disabled in the field, remove the port 22 rule.

Enable nftables:

```bash
sudo systemctl enable nftables
sudo systemctl restart nftables
```

## Services
Enable and start services:

```bash
sudo systemctl unmask hostapd
sudo systemctl enable hostapd dnsmasq
sudo systemctl restart hostapd dnsmasq
```

## Captive Portal Web Server
Run the captive portal web server on port 80. Example:

```bash
python3 web_server.py --config config.toml
```

## Validation Checklist
- Client can see SSID and join network.
- Client receives DHCP address in `10.0.0.0/24`.
- DNS resolves any hostname to `10.0.0.1`.
- `http://example.com/` loads the portal page.
- iOS/Android captive portal auto-opens (best-effort).
- Web UI is usable without radio attached.

## Troubleshooting
- If the AP does not appear: verify hostapd is running and `wlan0` exists.
- If DHCP fails: check dnsmasq logs and `wlan0` IP address.
- If portal does not load: confirm web server is bound to `0.0.0.0:80`.
- If DNS redirects fail: verify `address=/#/10.0.0.1` is active.

## Planned Work: Headless Pi Zero 2W Setup
This is a future stage to validate the setup on a fresh Pi Zero 2W without a monitor.
We expect to use USB gadget mode for configuration and verification.

Goals:
- Fully headless provisioning with no Ethernet.
- Document the gadget-mode steps and required kernel config changes.
- Verify AP + portal setup works on a clean image and survives reboot.

Artifacts to produce:
- Updated setup doc with gadget-mode steps.
- Provisioning script for repeatable multi-site deployments.
