#!/usr/bin/env bash
#
# setup_ap.sh — Configure Raspberry Pi as a WiFi captive portal access point
#
# This script configures the system networking stack to:
#   1. Broadcast an open WiFi network (for walk-up captive portal access)
#   2. Assign IP addresses to clients via DHCP
#   3. Redirect all DNS queries to the portal (captive portal detection)
#   4. Redirect port 80 traffic to port 8080 (so app can run unprivileged)
#   5. Firewall everything except portal services (and SSH for admin)
#
# Usage:
#   sudo ./setup_ap.sh --ssid "CivicMesh-Fremont" --channel 6
#   sudo ./setup_ap.sh --ssid "CivicMesh-Dev"  # channel defaults to 6
#   sudo ./setup_ap.sh --ssid "CivicMesh-Dev" --iface wlp2s0  # non-default interface
#   sudo ./setup_ap.sh --help
#
# Requirements:
#   - NetworkManager as the network management daemon
#   - WiFi hardware (default: wlan0, or specify with --iface)
#
# After running, reboot for changes to take effect.
#

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# =============================================================================
# Configuration Constants
# =============================================================================

# These are hardcoded because they're unlikely to vary per-deployment
readonly COUNTRY_CODE="US"
readonly SUBNET="10.0.0"           # We'll use 10.0.0.0/24
readonly AP_IP="${SUBNET}.1"
readonly DHCP_RANGE_START="${SUBNET}.50"
readonly DHCP_RANGE_END="${SUBNET}.150"
readonly DHCP_LEASE_TIME="12h"
readonly APP_PORT="8080"           # Port the app listens on (unprivileged)
readonly PUBLIC_PORT="80"          # Port clients connect to (redirected to APP_PORT)
readonly BACKUP_DIR="/var/backups/civicmesh"


# Default values for arguments
DEFAULT_CHANNEL=6
DEFAULT_SSID="CivicMesh-Dev"
DEFAULT_IFACE="wlan0"

# Track whether we've started making changes (for error handling)
CHANGES_STARTED=false

# =============================================================================
# Helper Functions
# =============================================================================

# Print usage information
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Configure this Raspberry Pi as a WiFi captive portal access point.

Options:
    --ssid NAME       WiFi network name (default: ${DEFAULT_SSID})
    --channel N       WiFi channel, use 1, 6, or 11 (default: ${DEFAULT_CHANNEL})
    --iface NAME      WiFi interface name (default: ${DEFAULT_IFACE})
    --help            Show this help message

Requirements:
    - NetworkManager running (script will check)
    - WiFi interface (wlan0, or specify with --iface)

Examples:
    sudo ./$(basename "$0") --ssid "CivicMesh-Fremont" --channel 6
    sudo ./$(basename "$0") --ssid "CivicMesh-Dev"
    sudo ./$(basename "$0") --ssid "CivicMesh-Dev" --iface wlp2s0

After running, reboot for changes to take effect:
    sudo reboot

EOF
}

# Print a section header
section() {
    echo ""
    echo "========================================"
    echo "$1"
    echo "========================================"
}

# Print an info message
info() {
    echo "[INFO] $1"
}

# Print a success message
ok() {
    echo "[ OK ] $1"
}

# Print a warning message
warn() {
    echo "[WARN] $1" >&2
}

# Print an error message and exit
die() {
    echo ""
    echo "[ERROR] $1" >&2
    if [[ "$CHANGES_STARTED" == "true" ]]; then
        echo ""
        echo "WARNING: Some changes may have been partially applied."
        echo "Check ${BACKUP_DIR} for .bak backups of modified files."
    fi
    exit 1
}

# Back up a file if it exists
backup_if_exists() {
    local file="$1"
    if [[ -f "$file" ]]; then
        mkdir -p "$BACKUP_DIR"
        local basename
        basename=$(basename "$file")
        local backup="${BACKUP_DIR}/${basename}.bak.$(date +%Y%m%d_%H%M%S)"
        info "Backing up existing ${file} to ${backup}"
        cp "$file" "$backup"
    fi
}

# Check if a systemd service is active
service_is_active() {
    systemctl is-active --quiet "$1" 2>/dev/null
}

# =============================================================================
# Argument Parsing
# =============================================================================

SSID="$DEFAULT_SSID"
CHANNEL="$DEFAULT_CHANNEL"
IFACE="$DEFAULT_IFACE"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ssid)
            if [[ -z "${2:-}" ]]; then
                die "--ssid requires a value"
            fi
            SSID="$2"
            shift 2
            ;;
        --channel)
            if [[ -z "${2:-}" ]]; then
                die "--channel requires a value"
            fi
            CHANNEL="$2"
            shift 2
            ;;
        --iface)
            if [[ -z "${2:-}" ]]; then
                die "--iface requires a value"
            fi
            IFACE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1 (use --help for usage)"
            ;;
    esac
done

# =============================================================================
# Pre-Flight Validation (ALL checks before ANY changes)
# =============================================================================

section "Pre-flight validation"

echo "Checking system requirements before making any changes..."
echo ""

VALIDATION_FAILED=false

# -----------------------------------------------------------------------------
# Check: Must be root
# -----------------------------------------------------------------------------
echo -n "Checking root privileges... "
if [[ $EUID -ne 0 ]]; then
    echo "FAILED"
    echo "  This script must be run as root (use sudo)"
    VALIDATION_FAILED=true
else
    echo "OK"
fi

# -----------------------------------------------------------------------------
# Check: NetworkManager is present and running
# -----------------------------------------------------------------------------
echo -n "Checking NetworkManager... "
if ! command -v nmcli &>/dev/null; then
    echo "FAILED"
    echo "  NetworkManager (nmcli) not found"
    echo "  This script requires NetworkManager to manage network interfaces"
    echo "  If your system uses dhcpcd instead, you'll need to adapt manually"
    VALIDATION_FAILED=true
elif ! service_is_active NetworkManager.service; then
    echo "FAILED"
    echo "  NetworkManager is installed but not running"
    echo "  Start it with: sudo systemctl start NetworkManager"
    VALIDATION_FAILED=true
else
    echo "OK"
fi

# -----------------------------------------------------------------------------
# Check: WiFi interface name safety
# -----------------------------------------------------------------------------
echo -n "Checking interface name safety... "
if ! [[ "$IFACE" =~ ^[a-zA-Z0-9_.:-]+$ ]]; then
    echo "FAILED"
    echo "  Interface name contains unsafe characters: ${IFACE}"
    echo "  Allowed pattern: ^[a-zA-Z0-9_.:-]+$"
    VALIDATION_FAILED=true
else
    echo "OK"
fi

# -----------------------------------------------------------------------------
# Check: WiFi interface exists
# -----------------------------------------------------------------------------
echo -n "Checking ${IFACE} interface... "
if ! ip link show "$IFACE" &>/dev/null; then
    echo "FAILED"
    echo "  Interface ${IFACE} not found"
    echo "  Available interfaces:"
    ip link show | grep -E '^\d+:' | awk '{print "    " $2}' | tr -d ':'
    echo ""
    echo "  Use --iface to specify a different WiFi interface"
    VALIDATION_FAILED=true
else
    echo "OK"
fi

# -----------------------------------------------------------------------------
# Check: WiFi not hardware-blocked
# -----------------------------------------------------------------------------
echo -n "Checking WiFi radio... "
if command -v rfkill &>/dev/null; then
    # rfkill types vary by system ("wlan", "wifi", "Wireless LAN", etc.)
    # Parse output looking for wireless entries
    if rfkill list | grep -A2 -i "wireless lan" | grep -qi "Hard blocked: yes"; then
        echo "FAILED"
        echo "  WiFi is hardware-blocked (physical switch?)"
        VALIDATION_FAILED=true
    elif rfkill list | grep -A2 -i "wireless lan" | grep -qi "Soft blocked: yes"; then
        echo "SOFT-BLOCKED (will unblock)"
    else
        echo "OK"
    fi
else
    echo "OK (rfkill not available, assuming unblocked)"
fi

# -----------------------------------------------------------------------------
# Check: SSID validity
# -----------------------------------------------------------------------------
echo -n "Checking SSID... "
if [[ -z "$SSID" ]]; then
    echo "FAILED"
    echo "  SSID cannot be empty"
    VALIDATION_FAILED=true
elif [[ ${#SSID} -gt 32 ]]; then
    echo "FAILED"
    echo "  SSID cannot be longer than 32 characters (got ${#SSID})"
    VALIDATION_FAILED=true
else
    echo "OK (\"${SSID}\")"
fi

# -----------------------------------------------------------------------------
# Check: Channel validity
# -----------------------------------------------------------------------------
echo -n "Checking channel... "
if ! [[ "$CHANNEL" =~ ^[0-9]+$ ]]; then
    echo "FAILED"
    echo "  Channel must be a number (got: ${CHANNEL})"
    VALIDATION_FAILED=true
elif [[ "$CHANNEL" -lt 1 || "$CHANNEL" -gt 11 ]]; then
    echo "FAILED"
    echo "  Channel must be between 1 and 11 for 2.4GHz (got: ${CHANNEL})"
    VALIDATION_FAILED=true
else
    if [[ "$CHANNEL" != "1" && "$CHANNEL" != "6" && "$CHANNEL" != "11" ]]; then
        echo "OK (${CHANNEL}, but 1/6/11 recommended)"
    else
        echo "OK (${CHANNEL})"
    fi
fi

# -----------------------------------------------------------------------------
# Check: Conflicting services
# -----------------------------------------------------------------------------
echo -n "Checking for conflicting services... "
CONFLICTS=""

# dhcpcd conflicts with systemd-networkd for interface management
if service_is_active dhcpcd.service 2>/dev/null; then
    CONFLICTS="${CONFLICTS}dhcpcd.service "
fi

# wpa_supplicant on the interface conflicts with hostapd
if service_is_active "wpa_supplicant@${IFACE}.service" 2>/dev/null; then
    CONFLICTS="${CONFLICTS}wpa_supplicant@${IFACE}.service "
fi

# systemd-resolved might bind port 53, conflicting with dnsmasq
if service_is_active systemd-resolved.service 2>/dev/null; then
    # Check if it's actually using port 53
    if ss -ulnp 2>/dev/null | grep -q ':53.*systemd-resolve'; then
        CONFLICTS="${CONFLICTS}systemd-resolved.service(port53) "
    fi
fi

if [[ -n "$CONFLICTS" ]]; then
    echo "FOUND"
    echo "  The following services may conflict and will be disabled:"
    echo "    ${CONFLICTS}"
else
    echo "OK"
fi

# -----------------------------------------------------------------------------
# Validation Summary
# -----------------------------------------------------------------------------
echo ""
if [[ "$VALIDATION_FAILED" == "true" ]]; then
    die "Pre-flight validation failed. No changes were made."
fi

ok "All pre-flight checks passed"

# =============================================================================
# Summary and Confirmation
# =============================================================================

section "Configuration summary"

cat <<EOF
The following changes will be made:

  WiFi Access Point:
    Interface:    ${IFACE}
    SSID:         ${SSID}
    Channel:      ${CHANNEL}
    IP Address:   ${AP_IP}
    DHCP Range:   ${DHCP_RANGE_START} - ${DHCP_RANGE_END}
    Country:      ${COUNTRY_CODE}

  Port Redirect:
    Clients connect to: port ${PUBLIC_PORT}
    App listens on:     port ${APP_PORT}

  Files to be created/modified:
    /etc/systemd/system/rfkill-unblock-wifi.service
    /etc/NetworkManager/conf.d/99-unmanaged-${IFACE}.conf
    /etc/systemd/network/20-${IFACE}-ap.network
    /etc/hostapd/hostapd.conf
    /etc/default/hostapd
    /etc/dnsmasq.d/civicmesh.conf
    /etc/nftables.conf

  Services to be enabled:
    rfkill-unblock-wifi, systemd-networkd, hostapd, dnsmasq, nftables

  WARNING: /etc/nftables.conf will be REPLACED entirely.
    Any existing firewall rules will be backed up but not preserved.
    This script assumes a dedicated CivicMesh device.

EOF

if [[ -n "$CONFLICTS" ]]; then
    echo "  Services to be disabled:"
    echo "    ${CONFLICTS}"
    echo ""
fi

# Prompt for confirmation
read -r -p "Proceed with these changes? [y/N] " response
case "$response" in
    [yY][eE][sS]|[yY])
        echo ""
        info "Proceeding with configuration..."
        ;;
    *)
        echo ""
        info "Aborted by user. No changes made."
        exit 0
        ;;
esac

# Mark that we're now making changes (for error messages)
CHANGES_STARTED=true

# =============================================================================
# Disable Conflicting Services
# =============================================================================

if [[ -n "$CONFLICTS" ]]; then
    section "Disabling conflicting services"

    if service_is_active dhcpcd.service 2>/dev/null; then
        info "Stopping and disabling dhcpcd..."
        systemctl stop dhcpcd.service
        systemctl disable dhcpcd.service
    fi

    if service_is_active "wpa_supplicant@${IFACE}.service" 2>/dev/null; then
        info "Stopping and disabling wpa_supplicant@${IFACE}..."
        systemctl stop "wpa_supplicant@${IFACE}.service"
        systemctl disable "wpa_supplicant@${IFACE}.service"
    fi

    # For systemd-resolved, we disable the stub listener rather than the whole service
    # AND we need to update /etc/resolv.conf to point to upstream DNS, not the stub
    if [[ "$CONFLICTS" == *"systemd-resolved"* ]]; then
        info "Configuring systemd-resolved to not bind port 53..."
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/no-stub.conf <<EOF
# CivicMesh: Disable stub listener so dnsmasq can use port 53
[Resolve]
DNSStubListener=no
EOF
        
        # Check if /etc/resolv.conf uses the stub resolver
        # This can happen in two ways:
        #   1. It's a symlink to stub-resolv.conf
        #   2. It contains 127.0.0.53 (the stub listener address)
        # In either case, we need to point to the upstream resolv.conf instead
        NEEDS_RESOLV_FIX=false
        
        if [[ -L /etc/resolv.conf ]]; then
            RESOLV_TARGET=$(readlink /etc/resolv.conf)
            if [[ "$RESOLV_TARGET" == *"stub"* ]]; then
                NEEDS_RESOLV_FIX=true
            fi
        fi
        
        if grep -q "127.0.0.53" /etc/resolv.conf 2>/dev/null; then
            NEEDS_RESOLV_FIX=true
        fi
        
        if [[ "$NEEDS_RESOLV_FIX" == "true" ]]; then
            info "Updating /etc/resolv.conf to use upstream DNS (not stub)..."
            # Remove existing file/symlink and point to the upstream resolv.conf
            # that systemd-resolved maintains (contains real upstream DNS servers)
            rm -f /etc/resolv.conf
            ln -s /run/systemd/resolve/resolv.conf /etc/resolv.conf
        fi
        
        systemctl restart systemd-resolved
    fi

    ok "Conflicting services handled"
fi

# =============================================================================
# Unblock WiFi (Now and Persistently at Boot)
# =============================================================================

section "Configuring WiFi rfkill unblock"

# WiFi is often soft-blocked at boot. We need to:
#   1. Disable systemd-rfkill (which restores "blocked" state and fights us)
#   2. Unblock WiFi now (if blocked)
#   3. Create a systemd service to unblock at every boot BEFORE hostapd starts
#
# Without this, hostapd fails with "rfkill: WLAN soft blocked"
#
# The systemd-rfkill problem:
#   - systemd-rfkill.socket watches /dev/rfkill for any activity
#   - When we run "rfkill unblock", it wakes up systemd-rfkill.service
#   - That service restores the saved state (blocked), undoing our unblock
#   - We can't win the ordering game, so we disable it entirely
#   - A dedicated AP has no use for rfkill state persistence anyway

if command -v rfkill &>/dev/null; then
    # Disable systemd-rfkill entirely (service AND socket)
    # The socket is the trigger - masking just the service isn't enough
    info "Masking systemd-rfkill to prevent rfkill state persistence..."
    systemctl mask systemd-rfkill.service systemd-rfkill.socket
    
    # Stop it if currently running
    systemctl stop systemd-rfkill.service 2>/dev/null || true
    systemctl stop systemd-rfkill.socket 2>/dev/null || true
    
    # Unblock now (idempotent - harmless if already unblocked)
    info "Unblocking WiFi..."
    rfkill unblock wifi

    # Also tell NetworkManager to enable WiFi radio
    # (NM has its own rfkill state file, separate from systemd-rfkill)
    if command -v nmcli &>/dev/null; then
        info "Enabling WiFi radio in NetworkManager..."
        nmcli radio wifi on
    fi
    
    # Create a systemd service to unblock at boot
    # Now that systemd-rfkill is masked, we only need to wait for the device node
    RFKILL_SERVICE="/etc/systemd/system/rfkill-unblock-wifi.service"
    
    info "Creating ${RFKILL_SERVICE} for persistent unblock at boot..."
    cat > "$RFKILL_SERVICE" <<'EOF'
[Unit]
Description=Unblock WiFi via rfkill at boot
DefaultDependencies=no
Before=hostapd.service
After=dev-rfkill.device
Requires=dev-rfkill.device

[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill unblock wifi
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable rfkill-unblock-wifi.service
    
    ok "WiFi rfkill unblock configured (systemd-rfkill disabled, unblock service enabled)"
else
    info "rfkill not available, skipping unblock configuration"
fi

# =============================================================================
# Package Installation
# =============================================================================

section "Installing required packages"

# Update package list
info "Updating package list..."
apt-get update -qq

# Install packages
# - apt-offline enables offline updates on devices without internet access
PACKAGES="hostapd dnsmasq nftables apt-offline"
info "Installing: ${PACKAGES}"
apt-get install -qq -y $PACKAGES

ok "Packages installed"

# =============================================================================
# NetworkManager Configuration
# =============================================================================

section "Configuring NetworkManager to ignore ${IFACE}"

# NetworkManager tries to manage all network interfaces by default.
# We need it to ignore the WiFi interface so hostapd can control it exclusively.
# NetworkManager will continue to manage eth0 (if present) for SSH access.

NM_CONF="/etc/NetworkManager/conf.d/99-unmanaged-${IFACE}.conf"

backup_if_exists "$NM_CONF"

info "Writing ${NM_CONF}"
cat > "$NM_CONF" <<EOF
# CivicMesh: Tell NetworkManager to ignore ${IFACE}
# ${IFACE} is managed by hostapd for the captive portal AP
[keyfile]
unmanaged-devices=interface-name:${IFACE}
EOF

info "Restarting NetworkManager to apply changes..."
systemctl restart NetworkManager

# Give it a moment to settle
sleep 2

# Verify interface is now unmanaged
if nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -q "^${IFACE}:unmanaged$"; then
    ok "${IFACE} is now unmanaged by NetworkManager"
else
    warn "Could not verify ${IFACE} is unmanaged. Continuing anyway."
fi

# =============================================================================
# systemd-networkd Configuration
# =============================================================================

section "Configuring systemd-networkd for static IP"

# systemd-networkd will assign a static IP to the WiFi interface.
# This is simpler than the old dhcpcd approach and is the modern way.

NETWORKD_CONF="/etc/systemd/network/20-${IFACE}-ap.network"

backup_if_exists "$NETWORKD_CONF"

info "Writing ${NETWORKD_CONF}"
cat > "$NETWORKD_CONF" <<EOF
# CivicMesh: Static IP configuration for ${IFACE} AP interface
#
# This assigns ${AP_IP}/24 to ${IFACE}.
# No gateway is specified because this interface IS the gateway for its network.

[Match]
Name=${IFACE}

[Network]
Address=${AP_IP}/24
EOF

# Enable systemd-networkd (it may not be enabled by default)
info "Enabling systemd-networkd service..."
systemctl enable systemd-networkd

ok "systemd-networkd configured"

# =============================================================================
# hostapd Configuration
# =============================================================================

section "Configuring hostapd (access point)"

# hostapd is the daemon that makes the WiFi interface broadcast as an access point.
# We configure an OPEN network (no password) for captive portal accessibility.

HOSTAPD_CONF="/etc/hostapd/hostapd.conf"
HOSTAPD_DEFAULT="/etc/default/hostapd"

backup_if_exists "$HOSTAPD_CONF"
backup_if_exists "$HOSTAPD_DEFAULT"

info "Writing ${HOSTAPD_CONF}"
cat > "$HOSTAPD_CONF" <<EOF
# CivicMesh: hostapd configuration for captive portal AP
#
# This configures an OPEN WiFi network (no password).
# Open networks are standard for captive portals because:
#   - No barrier for emergency walk-up access
#   - Captive portal detection works more reliably
#   - No security benefit anyway (HTTP-only portal, no internet)

# === Interface Configuration ===
interface=${IFACE}
driver=nl80211

# === Wireless Network Settings ===
ssid=${SSID}
hw_mode=g
channel=${CHANNEL}

# Enable 802.11n for better speeds (doesn't hurt on older hardware)
ieee80211n=1

# Disable WiFi Multimedia QoS (not needed for text-only portal)
wmm_enabled=0

# === Security: Open Network ===
# auth_algs=1 means "Open System" authentication (no challenge)
# wpa=0 disables WPA/WPA2 encryption entirely
auth_algs=1
wpa=0

# === Regulatory ===
# Country code is required for the radio to operate.
# This sets allowed channels and transmit power limits.
country_code=${COUNTRY_CODE}

# Advertise country code in beacons (required by some adapters)
ieee80211d=1
EOF

# Tell hostapd where to find its config file (Debian-specific requirement)
info "Writing ${HOSTAPD_DEFAULT}"
cat > "$HOSTAPD_DEFAULT" <<EOF
# CivicMesh: Point hostapd to its configuration file
DAEMON_CONF="/etc/hostapd/hostapd.conf"
EOF

# hostapd is masked by default on Debian. Unmask and enable it.
info "Unmasking and enabling hostapd service..."
systemctl unmask hostapd
systemctl enable hostapd

ok "hostapd configured"

# =============================================================================
# dnsmasq Configuration
# =============================================================================

section "Configuring dnsmasq (DHCP + DNS)"

# dnsmasq serves two roles:
#   1. DHCP server: assigns IP addresses to WiFi clients
#   2. DNS server: redirects ALL queries to the portal (captive portal trick)

DNSMASQ_CONF="/etc/dnsmasq.d/civicmesh.conf"

backup_if_exists "$DNSMASQ_CONF"

info "Writing ${DNSMASQ_CONF}"
cat > "$DNSMASQ_CONF" <<EOF
# CivicMesh: dnsmasq configuration for captive portal
#
# Provides DHCP (IP address assignment) and DNS (captive portal redirect)
# for clients connected to the WiFi AP.

# === Interface Binding ===
# Only listen on ${IFACE}, not on eth0 or localhost
interface=${IFACE}
bind-interfaces

# === DHCP Server ===
# Assign IPs from ${DHCP_RANGE_START} to ${DHCP_RANGE_END}
# This gives us ~100 addresses for walk-up clients
# Lease time: ${DHCP_LEASE_TIME}
dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},255.255.255.0,${DHCP_LEASE_TIME}

# Tell clients to use us as their gateway (for routing)
dhcp-option=option:router,${AP_IP}

# Tell clients to use us as their DNS server
dhcp-option=option:dns-server,${AP_IP}

# We are the only DHCP server on this network; be authoritative
# This reduces weirdness if a client thinks it's on a different LAN
dhcp-authoritative

# === DNS: Captive Portal Redirect ===
# The magic line: resolve ALL DNS queries to our IP
# The "/#/" syntax means "match any domain"
address=/#/${AP_IP}

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
EOF

# Ensure dnsmasq starts after the WiFi interface is ready
info "Configuring dnsmasq service ordering..."
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/dnsmasq.service.d/override.conf <<EOF
[Unit]
After=hostapd.service systemd-networkd.service
Wants=hostapd.service systemd-networkd.service
EOF
systemctl daemon-reload

# Enable dnsmasq
info "Enabling dnsmasq service..."
systemctl enable dnsmasq

ok "dnsmasq configured"

# =============================================================================
# Disable IPv6 on AP Interface
# =============================================================================

section "Disabling IPv6 on ${IFACE}"

# Phones increasingly try IPv6 first, which can cause confusing behavior when
# we're only serving IPv4. Explicitly disable IPv6 on the AP interface.

SYSCTL_CONF="/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"

backup_if_exists "$SYSCTL_CONF"

info "Writing ${SYSCTL_CONF}"
cat > "$SYSCTL_CONF" <<EOF
# CivicMesh: Disable IPv6 on the WiFi AP interface
# This reduces client weirdness since we only serve IPv4
net.ipv6.conf.${IFACE}.disable_ipv6 = 1
EOF

# Apply immediately (will also apply on boot via sysctl.d)
sysctl -w "net.ipv6.conf.${IFACE}.disable_ipv6=1" >/dev/null 2>&1 || true

ok "IPv6 disabled on ${IFACE}"

# =============================================================================
# nftables Configuration (Firewall + Port Redirect)
# =============================================================================

section "Configuring nftables (firewall + port redirect)"

# nftables does two things for us:
#
# 1. FIREWALL: Restrict what clients can do
#    - Allow DHCP, DNS, HTTP (portal services)
#    - Allow SSH on all interfaces (for admin access)
#    - Block everything else
#
# 2. PORT REDIRECT: Map port 80 to 8080
#    - Clients connect to port 80 (standard HTTP)
#    - nftables rewrites the destination port to 8080
#    - App listens on 8080 (unprivileged port)

NFTABLES_CONF="/etc/nftables.conf"

backup_if_exists "$NFTABLES_CONF"

info "Writing ${NFTABLES_CONF}"
cat > "$NFTABLES_CONF" <<EOF
#!/usr/sbin/nft -f
#
# CivicMesh: nftables firewall and port redirect rules
#
# This configuration:
#   1. Redirects port ${PUBLIC_PORT} -> ${APP_PORT} on ${IFACE} (so app runs unprivileged)
#   2. Allows only portal services (DHCP, DNS, HTTP) from WiFi clients
#   3. Allows SSH on all interfaces (for admin, including Pi Zero 2W which has no eth0)
#   4. Drops everything else

flush ruleset

# === NAT Table: Port Redirection (IPv4 only) ===
# Using 'ip' family, not 'inet', because NAT support for inet is inconsistent across kernels.
# Redirect incoming port ${PUBLIC_PORT} to ${APP_PORT} on ${IFACE}
# This allows the web server to run as an unprivileged user
table ip nat {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;

        # Redirect HTTP traffic on ${IFACE} from port ${PUBLIC_PORT} to ${APP_PORT}
        # Clients connect to port ${PUBLIC_PORT}, app receives on port ${APP_PORT}
        iifname "${IFACE}" tcp dport ${PUBLIC_PORT} redirect to :${APP_PORT}
    }
}

# === Filter Table: Firewall Rules ===
table inet filter {
    chain input {
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
        #   - Pi Zero 2W (deployment): SSH over ${IFACE} (no eth0 exists)
        # Security relies on SSH key authentication, not network restrictions.
        tcp dport 22 accept

        # HTTP: the portal web server (after NAT redirect, so port ${APP_PORT})
        tcp dport 80 accept
        tcp dport ${APP_PORT} accept

        # === ${IFACE}: WiFi AP clients ===
        # DHCP: clients requesting IP addresses (UDP port 67)
        iifname "${IFACE}" udp dport 67 accept

        # DNS: clients resolving domain names (UDP/TCP port 53)
        iifname "${IFACE}" udp dport 53 accept
        iifname "${IFACE}" tcp dport 53 accept


        # Everything else is dropped (default policy)
    }

    chain forward {
        type filter hook forward priority 0; policy drop;
        # No forwarding - this is not a router
        # WiFi clients cannot reach the internet or other networks
    }

    chain output {
        type filter hook output priority 0; policy accept;
        # Allow all outbound traffic from the Pi itself
        # (needed for apt, NTP, etc. via eth0 when available)
    }
}
EOF

# Enable nftables
info "Enabling nftables service..."
systemctl enable nftables

ok "nftables configured"

# =============================================================================
# Summary and Next Steps
# =============================================================================

section "Setup complete!"

cat <<EOF

Configuration files written:
  - ${NM_CONF}
  - ${NETWORKD_CONF}
  - ${HOSTAPD_CONF}
  - ${HOSTAPD_DEFAULT}
  - ${DNSMASQ_CONF}
  - ${NFTABLES_CONF}

Services enabled:
  - rfkill-unblock-wifi (ensures WiFi is unblocked at boot)
  - systemd-networkd (static IP for ${IFACE})
  - hostapd (WiFi access point)
  - dnsmasq (DHCP + DNS)
  - nftables (firewall + port redirect)

AP Configuration:
  - Interface:    ${IFACE}
  - SSID:         ${SSID}
  - Channel:      ${CHANNEL}
  - IP Address:   ${AP_IP}
  - DHCP Range:   ${DHCP_RANGE_START} - ${DHCP_RANGE_END}
  - Country:      ${COUNTRY_CODE}

Port Redirect:
  - Clients connect to: port ${PUBLIC_PORT}
  - App should listen on: port ${APP_PORT}

SSH Access:
  - Allowed on all interfaces (eth0 and ${IFACE})
  - Ensure SSH key authentication is configured!

EOF

# Security reminder about SSH
cat <<EOF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SECURITY NOTE
  
  SSH is accessible from the WiFi network. This is required for
  Pi Zero 2W (which has no ethernet port).
  
  Make sure password authentication is DISABLED in /etc/ssh/sshd_config:
    PasswordAuthentication no
  
  Only SSH key authentication should be allowed.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

cat <<EOF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  REBOOT REQUIRED
  
  Run: sudo reboot
  
  After reboot:
    1. The "${SSID}" network should appear in WiFi scans
    2. Connect a device to verify DHCP works
    3. Start your web server on port ${APP_PORT}
    4. Test captive portal detection on a phone
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Verification commands (run after reboot):
  systemctl status rfkill-unblock-wifi  # WiFi unblocked?
  systemctl status hostapd          # AP daemon running?
  systemctl status dnsmasq          # DHCP/DNS running?
  systemctl status nftables         # Firewall loaded?
  ip addr show ${IFACE}                # Has ${AP_IP}/24?
  cat /var/lib/misc/dnsmasq.leases  # Any connected clients?

Troubleshooting:
  rfkill list                       # WiFi blocked?
  journalctl -u hostapd -e          # AP errors
  journalctl -u dnsmasq -e          # DHCP/DNS errors
  sudo nft list ruleset             # Current firewall rules
  iw dev ${IFACE} info                 # WiFi interface state

EOF
