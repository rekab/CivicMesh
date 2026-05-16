"""Pure renderers: AppConfig -> bytes.

Each function takes the full AppConfig and returns the rendered file
content as UTF-8 bytes. No I/O, no logging, no subprocess. The seven
AP files match the output that the now-retired `scripts/setup_ap.sh`
produced for an equivalent config, byte-for-byte (with the noted
caveat that nftables drives its redirect target from cfg.web.port,
so for non-default ports the bytes diverge by design). The two
systemd units had no analogue in `setup_ap.sh` — they were new this
phase.

`flush ruleset` in render_nftables_conf is load-bearing: without it,
`nft -f` merges the new rules into the existing ruleset rather than
performing the atomic ruleset swap the apply pipeline depends on.
"""

from __future__ import annotations

from config import AppConfig


DEFAULT_FILE_MODE = 0o644


def render_hostapd_conf(cfg: AppConfig) -> bytes:
    return f"""\
# CivicMesh: hostapd configuration for captive portal AP
#
# This configures an OPEN WiFi network (no password).
# Open networks are standard for captive portals because:
#   - No barrier for emergency walk-up access
#   - Captive portal detection works more reliably
#   - No security benefit anyway (HTTP-only portal, no internet)

# === Interface Configuration ===
interface={cfg.network.iface}
driver=nl80211

# === Wireless Network Settings ===
ssid={cfg.ap.ssid}
hw_mode=g
channel={cfg.ap.channel}

# Enable 802.11n for better speeds (doesn't hurt on older hardware)
ieee80211n=1

# WMM (WiFi Multimedia QoS) must be enabled.
# 802.11n requires WMM per the spec. Disabling it causes iOS/iPadOS
# devices to refuse association (they see 802.11n advertised without
# the mandatory WMM capability and reject the network).
# Android tolerates wmm_enabled=0 by falling back to 802.11g, but
# iOS does not. Enabling WMM has negligible overhead on the Pi Zero 2W.
wmm_enabled=1

# === Security: Open Network ===
# auth_algs=1 means "Open System" authentication (no challenge)
# wpa=0 disables WPA/WPA2 encryption entirely
auth_algs=1
wpa=0

# === Regulatory ===
# Country code is required for the radio to operate.
# This sets allowed channels and transmit power limits.
country_code={cfg.network.country_code}

# Advertise country code in beacons (required by some adapters)
ieee80211d=1
""".encode("utf-8")


def render_hostapd_default(cfg: AppConfig) -> bytes:
    return b"""\
# CivicMesh: Point hostapd to its configuration file
DAEMON_CONF="/etc/hostapd/hostapd.conf"
"""


def render_dnsmasq_conf(cfg: AppConfig) -> bytes:
    return f"""\
# CivicMesh: dnsmasq configuration for captive portal
#
# Provides DHCP (IP address assignment) and DNS (captive portal redirect)
# for clients connected to the WiFi AP.

# === Interface Binding ===
# Only listen on {cfg.network.iface}, not on eth0 or localhost
interface={cfg.network.iface}
bind-interfaces

# === DHCP Server ===
# Assign IPs from {cfg.network.dhcp_range_start} to {cfg.network.dhcp_range_end}
# This gives us ~240 addresses for walk-up clients
# Lease time: {cfg.network.dhcp_lease}
dhcp-range={cfg.network.dhcp_range_start},{cfg.network.dhcp_range_end},255.255.255.0,{cfg.network.dhcp_lease}

# Tell clients to use us as their gateway (for routing)
dhcp-option=option:router,{cfg.network.ip}

# Tell clients to use us as their DNS server
dhcp-option=option:dns-server,{cfg.network.ip}

# RFC 8910 captive-portal-API option (114) intentionally NOT advertised:
# RFC 8908 requires the API URL be HTTPS, and this node serves HTTP only.
# Lenient clients that accepted an HTTP URL would receive captive=false and
# suppress the login flow entirely; strict clients ignore the option. Either
# way, we rely on the probe-redirect path (/generate_204 etc.) instead.

# We are the only DHCP server on this network; be authoritative
# This reduces weirdness if a client thinks it's on a different LAN
dhcp-authoritative

# === DNS: Captive Portal Redirect ===
# The magic line: resolve ALL DNS queries to our IP
# The "/#/" syntax means "match any domain"
address=/#/{cfg.network.ip}

# === Performance/Security ===
# Don't forward queries upstream (we're not a real DNS server)
no-resolv

# Don't watch /etc/resolv.conf for changes
no-poll

# No DNS cache needed (we return the same answer for everything)
cache-size=0

# === Debugging (uncomment if needed) ===
# Log all DHCP transactions (disable in production to reduce log noise)
#log-dhcp
""".encode("utf-8")


def render_networkd_conf(cfg: AppConfig) -> bytes:
    return f"""\
# CivicMesh: Static IP configuration for {cfg.network.iface} AP interface
#
# This assigns {cfg.network.ip}/24 to {cfg.network.iface}.
# No gateway is specified because this interface IS the gateway for its network.

[Match]
Name={cfg.network.iface}

[Network]
Address={cfg.network.ip}/24
""".encode("utf-8")


def render_nm_unmanaged_conf(cfg: AppConfig) -> bytes:
    return f"""\
# CivicMesh: Tell NetworkManager to ignore {cfg.network.iface}
# {cfg.network.iface} is managed by hostapd for the captive portal AP
[keyfile]
unmanaged-devices=interface-name:{cfg.network.iface}
""".encode("utf-8")


def render_nftables_conf(cfg: AppConfig) -> bytes:
    # PUBLIC_PORT=80 is hardcoded (the HTTP port the captive portal serves
    # on); APP_PORT comes from cfg.web.port (the unprivileged port the
    # Python server actually binds). The redirect rule sends 80 -> port.
    iface = cfg.network.iface
    port = cfg.web.port
    return f"""\
#!/usr/sbin/nft -f
#
# CivicMesh: nftables firewall and port redirect rules
#
# This configuration:
#   1. Redirects port 80 -> {port} on {iface} (so app runs unprivileged)
#   2. Allows only portal services (DHCP, DNS, HTTP) from WiFi clients
#   3. Allows SSH on all interfaces (for admin, including Pi Zero 2W which has no eth0)
#   4. Drops everything else

flush ruleset

# === NAT Table: Port Redirection (IPv4 only) ===
# Using 'ip' family, not 'inet', because NAT support for inet is inconsistent across kernels.
# Redirect incoming port 80 to {port} on {iface}
# This allows the web server to run as an unprivileged user
table ip nat {{
    chain prerouting {{
        type nat hook prerouting priority dstnat; policy accept;

        # Redirect HTTP traffic on {iface} from port 80 to {port}
        # Clients connect to port 80, app receives on port {port}
        iifname "{iface}" tcp dport 80 redirect to :{port}
    }}
}}

# === Filter Table: Firewall Rules ===
table inet filter {{
    chain input {{
        type filter hook input priority 0; policy drop;

        # Allow established/related connections (responses to our requests)
        ct state established,related accept

        # Allow all loopback traffic (localhost)
        iifname "lo" accept

        # Allow ICMP/ICMPv6 for diagnostics and to reduce client weirdness
        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept

        # === SSH: Allow on ALL interfaces ===
        # This is needed because:
        #   - Pi 4 (dev): SSH over eth0
        #   - Pi Zero 2W (deployment): SSH over {iface} (no eth0 exists)
        # Security relies on SSH key authentication, not network restrictions.
        tcp dport 22 accept

        # HTTP: the portal web server (after NAT redirect, so port {port})
        tcp dport 80 accept
        tcp dport {port} accept

        # === {iface}: WiFi AP clients ===
        # DHCP: clients requesting IP addresses (UDP port 67)
        iifname "{iface}" udp dport 67 accept

        # DNS: clients resolving domain names (UDP/TCP port 53)
        iifname "{iface}" udp dport 53 accept
        iifname "{iface}" tcp dport 53 accept

        # HTTPS/QUIC: reject fast on the AP iface
        # (see docs/captive-portal-precedent.md §4 "HTTPS: why we don't have it, and what follows")
        #
        # Wildcard DNS hijack means every background HTTPS request from a
        # connected phone resolves to {iface}'s IP. RST on TCP and ICMP
        # unreachable on UDP make client stacks fail fast instead of burning
        # battery on SYN retries / QUIC retransmits.
        iifname "{iface}" tcp dport 443 reject with tcp reset
        iifname "{iface}" udp dport 443 reject

        # Everything else is dropped (default policy)
    }}

    chain forward {{
        type filter hook forward priority 0; policy drop;
        # No forwarding - this is not a router
        # WiFi clients cannot reach the internet or other networks
    }}

    chain output {{
        type filter hook output priority 0; policy accept;
        # Allow all outbound traffic from the Pi itself
        # (needed for apt, NTP, etc. via eth0 when available)
    }}
}}
""".encode("utf-8")


def render_sysctl_conf(cfg: AppConfig) -> bytes:
    return f"""\
# CivicMesh: Disable IPv6 on the WiFi AP interface
# This reduces client weirdness since we only serve IPv4
net.ipv6.conf.{cfg.network.iface}.disable_ipv6 = 1
""".encode("utf-8")


def render_systemd_unit_web(cfg: AppConfig) -> bytes:
    return b"""\
[Unit]
Description=CivicMesh web server (captive portal)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=civicmesh
Group=civicmesh
WorkingDirectory=/usr/local/civicmesh/app
ExecStart=/usr/local/bin/civicmesh-web --config /usr/local/civicmesh/etc/config.toml
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
"""


def render_systemd_unit_mesh(cfg: AppConfig) -> bytes:
    return b"""\
[Unit]
Description=CivicMesh mesh bot (radio relay)

[Service]
Type=simple
User=civicmesh
Group=civicmesh
SupplementaryGroups=dialout
WorkingDirectory=/usr/local/civicmesh/app
ExecStart=/usr/local/bin/civicmesh-mesh --config /usr/local/civicmesh/etc/config.toml
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
"""
