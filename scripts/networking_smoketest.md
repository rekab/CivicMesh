# Networking Smoke Test

Run these checks after `setup_ap.sh` and a reboot.

## 1) Services up

```bash
systemctl status NetworkManager systemd-networkd hostapd dnsmasq nftables
```

Expected: all services `active (running)`.

## 2) WiFi interface has static IP

```bash
ip addr show <IFACE>
```

Expected: `${AP_IP}/24` on the interface.

## 3) NetworkManager is not managing the AP interface

```bash
nmcli device status | grep -E '<IFACE>.*unmanaged'
```

Expected: the interface shows `unmanaged`.

## 4) Firewall rules loaded

```bash
sudo nft list ruleset
```

Expected: `inet nat` rule redirecting port 80 -> 8080 on `<IFACE>` and filter rules for DHCP/DNS/HTTP.

## 5) DHCP + DNS from a client

- Connect a phone or laptop to the AP SSID.
- Verify it receives a `10.0.0.x` address in the configured range.

## 6) Captive portal behavior

- From the client, open any HTTP URL.
- Expected: redirected to the portal.

## 7) Portal app reachability

```bash
curl -I http://10.0.0.1
```

Expected: HTTP response from the captive portal server.

## 8) SSH (if enabled)

```bash
ssh <pi-user>@10.0.0.1
```

Expected: key-based login works; password auth is disabled.

## Troubleshooting quick checks

```bash
journalctl -u hostapd -e
journalctl -u dnsmasq -e
sudo nft list ruleset
```
