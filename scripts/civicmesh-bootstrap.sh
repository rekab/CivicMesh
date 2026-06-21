#!/usr/bin/env bash
#
# civicmesh-bootstrap.sh — Lay down /usr/local/civicmesh on a fresh Pi.
#
# One-shot, root-required. Idempotent on re-run: does not destroy
# config or DB, does not break running services. Stops at "venv built."
#
# Does NOT run `civicmesh configure` (needs operator input) and does
# NOT run `civicmesh apply` (needs configure first, and apply enables
# the systemd units). The next-steps banner tells the operator what
# to run next.
#

set -euo pipefail

# =============================================================================
# Constants
# =============================================================================

readonly CIVICMESH_REPO_URL="https://github.com/rekab/CivicMesh.git"
readonly CIVICMESH_USER="civicmesh"
readonly CIVICMESH_HOME="/usr/local/civicmesh"
readonly CIVICMESH_APP="${CIVICMESH_HOME}/app"
readonly CIVICMESH_ETC="${CIVICMESH_HOME}/etc"
readonly CIVICMESH_VAR="${CIVICMESH_HOME}/var"
readonly CIVICMESH_LOGS="${CIVICMESH_VAR}/logs"
readonly UV_BIN="${CIVICMESH_HOME}/.local/bin/uv"

# =============================================================================
# Helpers (lifted from scripts/setup_ap.sh; same shape across our scripts)
# =============================================================================

section() {
    echo ""
    echo "========================================"
    echo "$1"
    echo "========================================"
}

info() { echo "[INFO] $1"; }
ok()   { echo "[ OK ] $1"; }
warn() { echo "[WARN] $1" >&2; }
die()  { echo ""; echo "[ERROR] $1" >&2; exit 1; }

service_is_active() {
    systemctl is-active --quiet "$1" 2>/dev/null
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [--help]

Lay down /usr/local/civicmesh on a fresh Raspberry Pi.

Root-required, one-shot. Idempotent on re-run.

What it does (in order):
    1. apt install (git, curl, python3, hostapd, dnsmasq, nftables, rfkill, NetworkManager)
    2. Disable conflicting services (dhcpcd, systemd-resolved stub,
       persistently mask systemd-timesyncd and chrony for CIV-99
       clock-consensus invariant)
    3. rfkill unblock + persistent unblock-at-boot service
    4. Create the 'civicmesh' system user (home: ${CIVICMESH_HOME})
    5. Install uv as that user
    6. Clone ${CIVICMESH_REPO_URL} into ${CIVICMESH_APP}
    7. Build the prod venv (uv sync --frozen)
    8. Symlink civicmesh{,-web,-mesh} into /usr/local/bin
    9. Create /usr/local/civicmesh/{etc,var,var/logs}, chown to civicmesh
   10. Print the next-steps banner

Stops at "venv built." Run \`civicmesh configure\` and \`civicmesh apply\`
yourself afterwards (see the banner at the end).

Updates to an existing install go through \`civicmesh promote\` from
your dev tree, not by re-running this script.
EOF
}

# =============================================================================
# Argument parsing
# =============================================================================

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") ;;
    *) echo "[ERROR] unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

# =============================================================================
# Pre-flight (all checks before any changes)
# =============================================================================

section "Pre-flight"

[[ $EUID -eq 0 ]] || die "must be run as root (try: sudo bash $0)"
ok "running as root"

# Debian-family check via /etc/os-release.
if [[ ! -f /etc/os-release ]]; then
    die "/etc/os-release not found; can't verify OS family"
fi
# shellcheck source=/dev/null
. /etc/os-release
if [[ "${ID:-}" == "debian" || "${ID:-}" == "raspbian" ]]; then
    ok "OS: ${PRETTY_NAME:-${ID}}"
elif [[ " ${ID_LIKE:-} " == *" debian "* ]]; then
    ok "OS: ${PRETTY_NAME:-${ID}} (Debian-family via ID_LIKE)"
else
    die "this script targets Debian/Raspberry Pi OS (got ID=${ID:-unknown}, ID_LIKE=${ID_LIKE:-unknown})"
fi

# =============================================================================
# Step 1: apt install
# =============================================================================

section "Installing packages"

# NOTE: deliberately no apt-offline. CIV-64 dropped it; Pis without
# internet do their initial bootstrap from a one-time tethered/wired
# uplink (per Pi Imager pre-flight WiFi creds), not offline bundles.
apt-get update -qq
apt-get install -qq -y \
    git curl python3 python3-venv \
    hostapd dnsmasq nftables \
    rfkill network-manager \
    bluez
# bluez (bluetoothd + bluetoothctl) is only used by nodes with a Victron BMV
# battery monitor, but it's small and ships on Pi OS anyway; installing it
# unconditionally keeps `civicmesh apply`'s power-monitor branch from failing
# on a node that later gets a BMV. The radio itself stays blocked until apply
# enables it (see the rfkill-unblock-bluetooth unit below + _cmd_apply).
ok "packages installed"

# =============================================================================
# Step 2: Disable conflicting services
# =============================================================================

section "Disabling conflicting services"

# dhcpcd conflicts with the systemd-networkd config that `civicmesh
# apply` writes for the AP interface.
if service_is_active dhcpcd.service; then
    info "stopping and disabling dhcpcd..."
    systemctl stop dhcpcd.service
    systemctl disable dhcpcd.service
fi

# wpa_supplicant is left running. On a headless Pi imaged with the Pi
# Imager WiFi flow, wpa_supplicant is what holds the SSH session's wlan0
# association up — disabling it here would kill the session bootstrap is
# running over. `civicmesh apply` is what stages AP mode and disables
# wpa_supplicant for the next boot; the operator-issued reboot is the
# cutover.

# systemd-resolved binds :53 by default, which collides with dnsmasq.
# Disable just the stub listener (not the whole service) and repoint
# /etc/resolv.conf at the upstream resolver if it's currently aimed
# at the stub. Lifted from scripts/setup_ap.sh.
if service_is_active systemd-resolved.service; then
    info "configuring systemd-resolved to not bind port 53..."
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/civicmesh-no-stub.conf <<'EOF'
# CivicMesh: disable stub listener so dnsmasq can use port 53
[Resolve]
DNSStubListener=no
EOF

    # /etc/resolv.conf can point at the stub two ways: a symlink to
    # stub-resolv.conf, or contents naming 127.0.0.53. Either way,
    # repoint at /run/systemd/resolve/resolv.conf (real upstream).
    NEEDS_RESOLV_FIX=false
    if [[ -L /etc/resolv.conf ]]; then
        if [[ "$(readlink /etc/resolv.conf)" == *stub* ]]; then
            NEEDS_RESOLV_FIX=true
        fi
    fi
    if grep -q "127.0.0.53" /etc/resolv.conf 2>/dev/null; then
        NEEDS_RESOLV_FIX=true
    fi
    if [[ "$NEEDS_RESOLV_FIX" == "true" ]]; then
        info "repointing /etc/resolv.conf at upstream resolver..."
        rm -f /etc/resolv.conf
        ln -s /run/systemd/resolve/resolv.conf /etc/resolv.conf
    fi
    systemctl restart systemd-resolved
fi

# CIV-99: CivicMesh maintains its own corrected wall time by adding
# clock_state.offset_seconds to int(time.time()) on every stamped DB
# write. A separate NTP daemon stepping the OS clock underneath us
# breaks that model (the consensus task has a runtime external-step
# detector as a safety net, but persistent masking is the structural
# defense). `civicmesh apply` enforces this same invariant; doing it
# here keeps the deploy procedure from hitting an exit-7 between
# bootstrap and apply. Dev / RTC-backed nodes that want NTP back can
# `systemctl unmask <unit>` afterwards AND set `[clock]
# require_timesync_masked = false` in config.toml (which `civicmesh
# configure` prompts for; see docs/clock_consensus.md § "Dev / RTC
# machines"). systemctl mask is idempotent on re-runs.
for ntp_unit in systemd-timesyncd.service chrony.service; do
    if systemctl list-unit-files "$ntp_unit" >/dev/null 2>&1 \
       && systemctl list-unit-files "$ntp_unit" | grep -qE "^$ntp_unit"; then
        info "masking $ntp_unit (CivicMesh manages clock via offset-consensus)..."
        # mask refuses NEW starts; stop the unit if it's currently
        # running so we don't leave NTP racing with our offset
        # right after bootstrap.
        systemctl stop "$ntp_unit" 2>/dev/null || true
        systemctl mask "$ntp_unit"
    fi
done
ok "conflicting services handled"

# =============================================================================
# Step 3: rfkill setup
# =============================================================================

section "Configuring WiFi rfkill unblock"

# WiFi is often soft-blocked at boot. systemd-rfkill restores the
# saved "blocked" state and fights any unblock we do, so we mask it
# entirely (a dedicated AP has no use for rfkill state persistence)
# and install a oneshot unit that unblocks WiFi before hostapd starts.
# Lifted from scripts/setup_ap.sh.
if command -v rfkill &>/dev/null; then
    info "masking systemd-rfkill (service + socket)..."
    systemctl mask systemd-rfkill.service systemd-rfkill.socket
    systemctl stop systemd-rfkill.service 2>/dev/null || true
    systemctl stop systemd-rfkill.socket 2>/dev/null || true

    info "unblocking WiFi now..."
    rfkill unblock wifi
    if command -v nmcli &>/dev/null; then
        nmcli radio wifi on || true
    fi

    info "installing /etc/systemd/system/rfkill-unblock-wifi.service..."
    cat > /etc/systemd/system/rfkill-unblock-wifi.service <<'EOF'
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

    # Bluetooth unblock for the Victron BMV battery monitor. Installed here
    # (host-level unit, like the WiFi one above) but left DISABLED: unlike
    # WiFi, BT is only wanted on nodes with a battery monitor, so `civicmesh
    # apply` enables this unit iff [power_monitor].enabled and disables +
    # rfkill-blocks it otherwise (combo-chip coexistence + power). Ordered
    # after bluetooth.service so `bluetoothctl power on` has a running daemon.
    info "installing /etc/systemd/system/rfkill-unblock-bluetooth.service (disabled)..."
    cat > /etc/systemd/system/rfkill-unblock-bluetooth.service <<'EOF'
[Unit]
Description=Unblock + power Bluetooth for the CivicMesh battery monitor
After=bluetooth.service
Wants=bluetooth.service
Before=civicmesh-mesh.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/rfkill unblock bluetooth
ExecStartPost=-/usr/bin/bluetoothctl power on

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable rfkill-unblock-wifi.service
    # rfkill-unblock-bluetooth is intentionally NOT enabled here; apply owns it.
    ok "rfkill unblock configured"
else
    warn "rfkill not available; skipping (unusual on Pi OS)"
fi

# =============================================================================
# Step 4: Create civicmesh user
# =============================================================================

section "Creating civicmesh user"

if id -u "${CIVICMESH_USER}" &>/dev/null; then
    info "user ${CIVICMESH_USER} already exists; skipping"
else
    # -m makes ${CIVICMESH_HOME} the home dir, owned by civicmesh.
    useradd -r -m -d "${CIVICMESH_HOME}" -s /bin/bash "${CIVICMESH_USER}"
    ok "user ${CIVICMESH_USER} created"
fi

# useradd -m on modern Debian (trixie / RPi OS bookworm-derived) creates
# the home dir mode 0700 — only the civicmesh user can traverse
# /usr/local/civicmesh, which blocks `civicmesh promote` run as the
# operator's own login from a same-host dev checkout. Set 0755 so any
# user can traverse the top-level dir; sub-paths (etc/config.toml, var/)
# keep their own owner+mode, so file-level protection is unchanged.
# Idempotent — runs every bootstrap, including re-runs that take the
# `id -u` short-circuit above.
chmod 755 "${CIVICMESH_HOME}"

# =============================================================================
# Step 5: Install uv as civicmesh
# =============================================================================

section "Installing uv"

if [[ -x "${UV_BIN}" ]]; then
    info "uv already installed at ${UV_BIN}; skipping"
else
    # The astral installer puts uv at ~/.local/bin/uv. We use the
    # absolute UV_BIN path everywhere downstream because `sudo -u`
    # doesn't load login profiles, so PATH won't include ~/.local/bin.
    sudo -u "${CIVICMESH_USER}" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    if [[ ! -x "${UV_BIN}" ]]; then
        die "uv install ran but ${UV_BIN} is not executable"
    fi
    ok "uv installed at ${UV_BIN}"
fi

# =============================================================================
# Step 6: Clone the repo
# =============================================================================

section "Cloning CivicMesh"

if [[ -d "${CIVICMESH_APP}/.git" ]]; then
    info "${CIVICMESH_APP} already contains a git tree; skipping clone"
    info "to update an existing install, run 'uv run civicmesh promote --from .' from your dev tree"
else
    sudo -u "${CIVICMESH_USER}" git clone "${CIVICMESH_REPO_URL}" "${CIVICMESH_APP}"
    ok "cloned into ${CIVICMESH_APP}"
fi

# =============================================================================
# Step 7: Build the prod venv
# =============================================================================

section "Building prod venv"

# Run unconditionally — fast no-op if venv is current; picks up
# post-clone work otherwise.
sudo -u "${CIVICMESH_USER}" sh -c "cd '${CIVICMESH_APP}' && '${UV_BIN}' sync --frozen"
ok "venv built at ${CIVICMESH_APP}/.venv"

# =============================================================================
# Step 8: Symlink entry-point scripts
# =============================================================================

section "Installing /usr/local/bin symlinks"

ln -sf "${CIVICMESH_APP}/.venv/bin/civicmesh"      /usr/local/bin/civicmesh
ln -sf "${CIVICMESH_APP}/.venv/bin/civicmesh-web"  /usr/local/bin/civicmesh-web
ln -sf "${CIVICMESH_APP}/.venv/bin/civicmesh-mesh" /usr/local/bin/civicmesh-mesh
ok "symlinks installed"

# =============================================================================
# Step 9: Directory tree
# =============================================================================

section "Creating directory tree"

# civicmesh-tool.md spec defines etc/ (config) and var/ (db, logs,
# marker files). promote writes its marker to var/last-promoted-commit.
mkdir -p "${CIVICMESH_ETC}" "${CIVICMESH_VAR}" "${CIVICMESH_LOGS}"
chown -R "${CIVICMESH_USER}:${CIVICMESH_USER}" "${CIVICMESH_HOME}"
ok "${CIVICMESH_HOME}/{etc,var,var/logs} ready"

# =============================================================================
# Step 10: Next-steps banner
# =============================================================================

echo ""
echo "Bootstrap complete."
echo ""
echo "NTP is masked: systemd-timesyncd and chrony are off so they don't"
echo "fight CivicMesh's wall-clock correction (docs/clock_consensus.md)."
echo ""
echo "  - Production / deployed nodes: leave it. Walk-up phones provide"
echo "    time via the captive portal; \`civicmesh stats\` shows the"
echo "    corrected offset."
echo "  - Development machines that want NTP back: during \`civicmesh"
echo "    configure\`, answer \"yes\" to the \"keep NTP running?\" prompt,"
echo "    then \`sudo systemctl unmask systemd-timesyncd.service\` (and"
echo "    chrony.service if installed)."
echo ""
echo "Next:"
echo "  sudo -u civicmesh civicmesh configure   # write the config interactively"
echo "  sudo civicmesh apply                    # render system files, start services"
echo ""
