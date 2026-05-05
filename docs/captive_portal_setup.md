# Captive portal setup (Raspberry Pi OS)

The captive-portal AP stack is set up automatically by
`scripts/civicmesh-bootstrap.sh` and `civicmesh apply` — see the
[deployment flow](../README.md#deployment) for the procedure.

This doc is a reference for what those tools produce and why. The
pure renderers live in
[`apply/renderers.py`](../apply/renderers.py); the on-disk paths
they target are in [`apply/driver.py`](../apply/driver.py).

## Target platform

- Raspberry Pi OS Lite (current release).
- Pi 4 for development, Pi Zero 2W for deployment.
- HTTP-only portal, no internet uplink.

## Before you begin

Run `apt full-upgrade` and reboot before provisioning. Older
Raspberry Pi OS kernels have a `brcmfmac` P2P crash triggered by
nearby iOS devices. `apt upgrade` (without `full-upgrade`) is not
sufficient on Raspberry Pi OS because it holds back kernel
packages.

```bash
sudo apt update
sudo apt full-upgrade
sudo reboot
```

(Bootstrap installs packages but does not currently force this
upgrade — see the open question on
[brcmfmac on Pi Zero 2W](open_questions.md).)

## Packages

`scripts/civicmesh-bootstrap.sh` installs:

- `hostapd` — AP daemon
- `dnsmasq` — DHCP + DNS server (with the captive-portal redirect)
- `nftables` — firewall + NAT port redirect
- `network-manager` — used to selectively *unmanage* the AP iface
- `rfkill` — keeps WiFi unblocked at boot

Bootstrap also disables `dhcpcd` (replaced by `systemd-networkd`
for the AP interface) and configures `systemd-resolved` not to
bind the DNS stub on port 53 (which would collide with `dnsmasq`).

## hostapd — `/etc/hostapd/hostapd.conf`

Renderer: `render_hostapd_conf` (apply/renderers.py:25). Drives off
`network.iface`, `ap.ssid`, `ap.channel`, `network.country_code`.

Key choices:

- **Open network** (`wpa=0`, `auth_algs=1`). Standard for emergency
  walk-up access; HTTP-only portal anyway, nothing to protect.
- **WMM enabled** (`wmm_enabled=1`). 802.11n requires WMM per the
  spec; iOS/iPadOS see 802.11n advertised without WMM and refuse
  to associate. Android tolerates this; iOS does not.
- **`ieee80211n=1`** for 802.11n speeds.
- **`country_code` + `ieee80211d=1`** required by some adapters
  before the radio will operate.

A pointer file (`render_hostapd_default` →
`/etc/default/hostapd`) sets `DAEMON_CONF` so `hostapd.service`
finds the rendered config.

## dnsmasq — `/etc/dnsmasq.d/civicmesh.conf`

Renderer: `render_dnsmasq_conf` (apply/renderers.py:78). Drives off
`network.iface`, `network.ip`, `network.dhcp_range_start/end`,
`network.dhcp_lease`.

Key knobs:

- `interface={iface}` + `bind-interfaces` — listen only on the AP
  iface, not on `eth0` or `lo`.
- `address=/#/{ip}` — the captive-portal DNS hijack: every name
  resolves to the portal IP.
- `dhcp-range`, `dhcp-option=router`, `dhcp-option=dns-server` —
  hand out leases on `{ip}/24`, point clients at the portal for
  both gateway and DNS.
- `dhcp-authoritative` — we are the only DHCP server on this
  network; cuts client hesitation on responses.
- `no-resolv`, `no-poll`, `cache-size=0` — we are not a real DNS
  server, no upstream forwarder, same answer for everything.

The RFC 8910 captive-portal-API option (114) is intentionally
**not** advertised. RFC 8908 requires the API URL be HTTPS; this
node serves HTTP only. We rely on the probe-redirect path
(`/generate_204` etc.) instead.

## Networking — systemd-networkd, NetworkManager unmanage, sysctl

Three small files cover the IP / iface management piece:

- `render_networkd_conf` →
  `/etc/systemd/network/20-{iface}-ap.network`. Assigns
  `{ip}/24` to the AP interface. No gateway — this iface *is*
  the gateway for its network.
- `render_nm_unmanaged_conf` →
  `/etc/NetworkManager/conf.d/99-unmanaged-{iface}.conf`. Tells
  NetworkManager to keep hands off `{iface}` so hostapd has
  exclusive control.
- `render_sysctl_conf` →
  `/etc/sysctl.d/90-civicmesh-disable-ipv6.conf`. Disables IPv6
  on `{iface}` to reduce client weirdness — the portal is
  IPv4-only.

## nftables — `/etc/nftables.conf`

Renderer: `render_nftables_conf` (apply/renderers.py:157). The
most load-bearing piece for portal behavior; it does three things.

### 1. NAT redirect 80 → `cfg.web.port`

```nft
table ip nat {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "{iface}" tcp dport 80 redirect to :{port}
    }
}
```

The web server runs as the unprivileged `civicmesh` user and binds
`cfg.web.port` (default `8080`). Clients connect to `:80`; the
prerouting redirect rewrites the destination to `:{port}` before
the filter table sees the packet. **The web server is not bound
to port 80** — anything that says it is, is wrong.

`ip nat` (not `inet nat`) is deliberate: NAT support for the
`inet` family is inconsistent across kernels, and we only need
IPv4 anyway since IPv6 is disabled on the AP iface.

### 2. Filter rules

```nft
table inet filter {
    chain input {
        type filter hook input priority 0; policy drop;

        ct state established,related accept
        iifname "lo" accept

        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept

        # SSH on all interfaces. Pi Zero 2W has no eth0, so SSH
        # over the AP iface is the only way in. Auth is by key.
        tcp dport 22 accept

        # Portal: clients hit :80, NAT redirected to :{port}.
        # Both rules needed because the redirect happens before
        # filter.
        tcp dport 80 accept
        tcp dport {port} accept

        # WiFi-only services
        iifname "{iface}" udp dport 67 accept   # DHCP
        iifname "{iface}" udp dport 53 accept   # DNS
        iifname "{iface}" tcp dport 53 accept

        # HTTPS/QUIC reject-fast on the AP iface.
        iifname "{iface}" tcp dport 443 reject with tcp reset
        iifname "{iface}" udp dport 443 reject
    }

    chain forward { type filter hook forward priority 0; policy drop; }
    chain output  { type filter hook output  priority 0; policy accept; }
}
```

The HTTPS/QUIC reject is from the iOS captive-portal precedent
work; see [docs/captive-portal-precedent.md](captive-portal-precedent.md)
§4. The DNS hijack means every background TLS handshake on a
connected phone resolves to the portal IP, so a hard `RST` on TCP
443 and ICMP unreachable on UDP 443 fail clients fast instead of
burning their battery on retries.

### 3. `flush ruleset` at the top

Without it, `nft -f` merges new rules into the existing ruleset
instead of doing the atomic swap that `apply` relies on for safe
reloads. Don't remove it.

## systemd units

Two service units, also rendered by apply:

- `render_systemd_unit_web` → `/etc/systemd/system/civicmesh-web.service`.
  Runs `/usr/local/bin/civicmesh-web` as user `civicmesh`. Binds
  `cfg.web.port`.
- `render_systemd_unit_mesh` → `/etc/systemd/system/civicmesh-mesh.service`.
  Runs `/usr/local/bin/civicmesh-mesh` as user `civicmesh` with
  `SupplementaryGroups=dialout` for serial-port access.

Both use `Restart=on-failure`, `RestartSec=5`,
`StartLimitBurst=5`, `StartLimitIntervalSec=60` to cap restart
storms. `civicmesh apply` enables and (re)starts them.

## Validation

After `civicmesh apply` and a reboot:

- Client sees the configured SSID and joins.
- Client receives a DHCP lease in the configured range.
- DNS resolves any name to the portal IP.
- `http://anything/` (or directly the portal IP) loads the portal.
- iOS/Android captive-portal detection auto-opens the portal.
- `civicmesh stats` returns counters.

## Troubleshooting

- AP doesn't appear: `systemctl status hostapd`; check
  `/sys/class/net/{iface}` exists and is up.
- Clients connect but no DHCP: `systemctl status dnsmasq`;
  `journalctl -u dnsmasq -f` while a client connects.
- Portal page doesn't load: confirm the nftables NAT redirect is
  active (`sudo nft list ruleset | grep redirect`) and the web
  service is up (`systemctl status civicmesh-web`).
- DNS hijack not working: check `address=/#/{ip}` is in the
  rendered `/etc/dnsmasq.d/civicmesh.conf`.
- iOS connects, gets DHCP, then Safari hangs loading the portal:
  the page may be redirecting to a `.local` hostname. iOS treats
  `.local` as mDNS-only and bypasses unicast DNS, so the dnsmasq
  hijack doesn't help. See
  [docs/ios-captive-portal-notes.md](ios-captive-portal-notes.md).
- iPad briefly connects then disconnects, or `dmesg` shows
  `brcmf_p2p_send_action_frame` oopses: known Broadcom WiFi
  driver bug triggered by iOS Wi-Fi Direct chatter. Run
  `sudo apt full-upgrade` and reboot. See
  [docs/ios-captive-portal-notes.md](ios-captive-portal-notes.md).

For a broader debugging ladder (systemctl → journalctl → app logs
→ lsof → ttyUSB → recovery characterization), see the
[service-debugging section](civicmesh-tool.md#troubleshooting-a-failing-service)
of the operator tool reference.
