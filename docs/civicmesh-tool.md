# civicmesh(1) — Operator tool for CivicMesh nodes

Reference for the `civicmesh` command-line tool. Covers configuration,
deployment (dev → prod), and runtime operations.

This document is the source of truth for the next round of implementation
work and is intended to be handed to Claude Code as input.

---

## STATUS LEGEND

Every command in this doc carries one of:

| Marker | Meaning |
|---|---|
| `[IMPL]` | Implemented today (in `admin.py`). Will need to be ported / extended. |
| `[NEW]`  | Proposed in this doc. Does not exist yet. |
| `[GAP]`  | UI-vs-admin drift. Listed for awareness; **not** part of this round. |

`[IMPL]` and `[NEW]` are fully specified in the body. `[GAP]` items appear
only in "Known gaps" near the end.

---

## NAME

**civicmesh** — single entry point for configuring, deploying, and
operating a CivicMesh node.

Replaces the current `civicmesh-admin` entry point. Service entry points
(`civicmesh-web`, `civicmesh-mesh`) are unchanged.

---

## DEV vs PROD: BRIGHT-LINE MODES

**This is the most important section of the doc. The mode model
constrains every other design decision.**

CivicMesh runs in exactly one of two modes at any given invocation, and
the mode is determined entirely by where the binary lives on disk. There
is no config setting, no env var, no flag that selects the mode. The
filesystem is the source of truth.

### The two modes

| | DEV | PROD |
|---|---|---|
| Project tree | anywhere not under `/usr/local/civicmesh/` (typically `~/code/CivicMesh/` or similar) | `/usr/local/civicmesh/app/` |
| Owner | the developer's user account | dedicated `civicmesh` system user |
| Binary path | `<project_root>/.venv/bin/civicmesh` | `/usr/local/civicmesh/app/.venv/bin/civicmesh` (also reachable via `/usr/local/bin/civicmesh` symlink) |
| Config | `<project_root>/config.toml` | `/usr/local/civicmesh/etc/config.toml` |
| Database | `<project_root>/civic_mesh.db` (or as set in local config) | `/usr/local/civicmesh/var/civic_mesh.db` |
| Logs | `<project_root>/logs/` | `/usr/local/civicmesh/var/logs/` |
| Services managed by systemd | no | yes (`civicmesh-web`, `civicmesh-mesh`) |
| `apply` (real) | refused | yes (root required) |
| `apply --dry-run` | yes (renders to tmpdir, diffs against `/etc/`, never writes) | yes (same behavior) |
| `promote` | yes (the only command that crosses) | refused |

### How the binary detects its mode

At startup, before any command logic runs:

```python
from pathlib import Path
import sys

binary = Path(sys.argv[0]).resolve()
mode = "prod" if str(binary).startswith("/usr/local/civicmesh/") else "dev"
```

That's the whole rule. No env-var sniffing, no PATH inspection, no
heuristics.

### How the binary cannot be confused

`[project.scripts]` in `pyproject.toml` causes `uv sync` (and `pip
install`) to generate a Python launcher script in `.venv/bin/civicmesh`.
The shebang of that launcher is the venv's absolute Python path, e.g.,
`#!/usr/local/civicmesh/app/.venv/bin/python`. The kernel honors that
shebang regardless of `$PATH`, `$PYTHONPATH`, current working directory,
or active venv.

Consequences:

- Prod launcher *always* runs prod's Python with prod's `site-packages`.
- Dev launcher *always* runs dev's Python with dev's `site-packages`.
- The mode-detection check inside the code is a belt-and-suspenders
  confirmation; the shebang already enforces the separation.

This also means **promote cannot rsync `.venv/`** — the shebangs would
have stale paths. Promote rsyncs source files only and re-runs `uv sync`
in the destination tree to regenerate launchers with prod's paths.

### Refusal rules

Each mode actively refuses to operate on the other's state:

| Mode | Refuses if | Error message says |
|---|---|---|
| DEV | `--config` resolves to a path under `/usr/local/civicmesh/` | "this is the dev binary; use the prod binary at `/usr/local/bin/civicmesh` for that path" |
| DEV | `VIRTUAL_ENV` is set and is not `<project_root>/.venv/` | "VIRTUAL_ENV points elsewhere; either unset it or `cd` into the dev tree" |
| PROD | `--config` resolves to a path *not* under `/usr/local/civicmesh/` | "this is the prod binary; it operates only on `/usr/local/civicmesh/etc/config.toml`" |
| PROD | `VIRTUAL_ENV` is set and is not `/usr/local/civicmesh/app/.venv/` | "VIRTUAL_ENV points elsewhere; open a clean shell or unset" |
| DEV | command is `apply` (real, not `--dry-run`) | "apply runs only in prod; use `apply --dry-run` to preview, or `promote` to deploy" |
| PROD | command is `promote` | "promote must run from a dev checkout; `cd` to your dev tree and run `uv run civicmesh promote --from .`" |

Refusal exits non-zero (specifically exit code 10, "wrong-mode") with a
message that names the offending path/var and the corrective action.

### Why the `VIRTUAL_ENV` check

`VIRTUAL_ENV` is set when:
- a venv was activated with `source .venv/bin/activate`
- `uv run` is in the call chain
- some other tooling pre-set it

If a user activates dev's venv and types `civicmesh` (which resolves
through PATH to the prod symlink), without the `VIRTUAL_ENV` check the
prod launcher would still run prod code (correct, due to shebang) but
the user clearly intended a dev operation. The check converts this
silent-correct behavior into a loud refusal that says "you probably
meant `uv run civicmesh` from `~/code/CivicMesh/`."

It also catches the mirror case: someone shells into
`/usr/local/civicmesh/app/` and types `uv run civicmesh ...` — that
would set `VIRTUAL_ENV` to the prod venv, the prod binary would run, and
everything would work, but this is not a supported invocation pattern
and we want it to fail loudly rather than silently work.

---

## RUNNING DEV ALONGSIDE PROD

Stop prod services before running dev. Prod and dev share two physical
resources on the host — `/dev/ttyUSB0` (the radio) and the web TCP port
configured in `[web].port` — and the serial port collision is silent.

### Why this is needed

Every other piece of state is already isolated by the dev/prod split:
separate venvs, separate `config.toml`, separate `civic_mesh.db`,
separate log directories. The radio device node and the bound TCP port
are the exceptions — they are physical singletons on the host. Only one
process at a time can use either correctly.

### The recipe

```
sudo systemctl stop civicmesh-mesh civicmesh-web
# ...do dev work, e.g.:
#   uv run civicmesh-web  --config config.toml
#   uv run civicmesh-mesh --config config.toml
sudo systemctl start civicmesh-mesh civicmesh-web
```

### What goes wrong if you skip the stop step

The two ports fail very differently when contended:

- **Web port (`[web].port`): loud failure, no state damage.** The
  second process exits immediately with
  `OSError: [Errno 98] Address already in use`. Easy to diagnose;
  nothing else is affected.
- **Serial port (`/dev/ttyUSB0`): silent failure, real damage.** Linux
  does not exclusive-lock USB-serial nodes; pyserial opens without
  `TIOCEXCL`. Both processes' `open()` calls return success. Inbound
  radio bytes split arbitrarily between the two readers; outbound
  writes from the two processes interleave on the wire. Symptoms
  mimic radio hardware flakiness (corrupt frames, command timeouts,
  missed `RX_LOG_DATA` events) and trigger spurious recovery actions
  in `RecoveryController` — RTS resets, reconnects, the works. None
  of those help, because the duplicate file descriptors survive an
  ESP32 reset.

For the engineering view of this failure mode, see **C3** in
`docs/radio-debugging/failure-modes.md`. The unambiguous diagnostic
signal is `lsof /dev/ttyUSB0` (or `fuser /dev/ttyUSB0`) returning
multiple PIDs. Without that check, C3 looks identical to the genuine
framing-desync modes C1 and C2.

### The dev-running-then-prod-started case

`sudo systemctl start civicmesh-mesh` while a dev `mesh_bot` is already
holding `/dev/ttyUSB0` is especially nasty. Systemd's restart policy
masks the corruption: the unit cycles in `activating (auto-restart)`
rather than failing cleanly, so the operator sees "service is starting"
rather than a clear `EADDRINUSE`-style error. Symptoms on the dev side
look like radio flakiness; symptoms on the prod side look like a unit
that "won't quite come up."

If symptoms suggest C3, verify with:

```
systemctl is-active civicmesh-mesh
journalctl -u civicmesh-mesh -e
lsof /dev/ttyUSB0
```

A flapping unit, repeated framing/timeout errors in the journal, and
multiple PIDs in `lsof` together confirm C3.

---

## SYNOPSIS

```
DEV:   uv run civicmesh <command> [<args>]      (from project root)
PROD:  civicmesh <command> [<args>]             (from anywhere)
       sudo civicmesh <command> [<args>]        (when root is required)
```

Most commands accept `--config PATH` to override the default config
location, subject to the refusal rules above.

---

## COMMAND OVERVIEW

```
Setup (one-time per node):
  bootstrap           Fresh-install entry point. Shell script, not a       [NEW]
                      civicmesh subcommand. See "Bootstrap" section.

Configuration:
  configure           Write or update config.toml interactively.           [NEW]
  config show         Print the parsed effective config.                   [NEW]
  config validate     Parse config.toml and report errors.                 [NEW]

Deployment:
  apply               Render system files from config.toml. Prod only.     [NEW]
  apply --dry-run     Preview what apply would change. Both modes.         [NEW]
  promote             Push dev tree to prod tree.                          [NEW]
                      Dev-mode only. Pass --restart to also restart
                      services (default: leaves services running on
                      the old code; operator picks the cutover moment).

Hub-docs releases:
  # A separate category from Deployment: content updates (curated
  # PDFs) ship on their own cadence — typically more often than
  # system/config deployment.
  install-hub-docs    Install a hub-docs release zip; atomic swap.         [NEW]
  rollback-hub-docs   Roll the hub-docs symlink back to a prior release.   [NEW]

Operations (runtime):
  stats               Print message/session/outbox/vote counts.            [IMPL]
  cleanup             Run retention cleanup.                               [IMPL]
  messages recent     List recent messages.                                [IMPL]
  outbox list         List pending outbox messages.                        [IMPL]
  outbox cancel       Cancel one queued outbox message.                    [IMPL]
  outbox clear        Cancel all queued outbox messages.                   [IMPL]
  sessions list       List recent WiFi sessions.                           [IMPL]
  sessions show       Show one session's detail.                           [IMPL]
  sessions reset      Zero a session's hourly post counter.                [IMPL]
  pin / unpin         Pin or unpin a message.                              [IMPL]
```

---

## FILESYSTEM LAYOUT (PROD)

`/usr/local/civicmesh/` is the self-contained tree. Everything CivicMesh
owns lives under it.

```
/usr/local/civicmesh/                         # owned by civicmesh:civicmesh
├── app/                                      # the git checkout (only thing promote replaces)
│   ├── .venv/                                # uv-managed; rebuilt by promote
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── web_server.py
│   ├── mesh_bot.py
│   ├── civicmesh.py                          # was admin.py
│   ├── config.py
│   ├── database.py
│   ├── ...
│   └── static/
├── etc/
│   └── config.toml                           # written by `civicmesh configure`
└── var/
    ├── civic_mesh.db
    ├── feedback.jsonl
    └── logs/
        ├── web_server.log
        ├── mesh_bot.log
        └── security.log
```

System-integration files live in their conventional locations and are
rendered by `civicmesh apply`:

```
/usr/local/bin/civicmesh                      → symlink to /usr/local/civicmesh/app/.venv/bin/civicmesh
/usr/local/bin/civicmesh-web                  → symlink (used by systemd unit)
/usr/local/bin/civicmesh-mesh                 → symlink (used by systemd unit)

/etc/systemd/system/civicmesh-web.service     # rendered by `apply`
/etc/systemd/system/civicmesh-mesh.service    # rendered by `apply`
/etc/systemd/system/rfkill-unblock-wifi.service  # written by bootstrap, not apply

/etc/hostapd/hostapd.conf                     # rendered by `apply`
/etc/default/hostapd                          # rendered by `apply`
/etc/dnsmasq.d/civicmesh.conf                 # rendered by `apply`
/etc/systemd/network/20-<iface>-ap.network    # rendered by `apply`; filename embeds iface
/etc/NetworkManager/conf.d/99-unmanaged-<iface>.conf  # rendered by `apply`
/etc/nftables.conf                            # rendered by `apply` (replaces wholesale)
/etc/sysctl.d/90-civicmesh-disable-ipv6.conf  # rendered by `apply`
```

Uninstall is `rm -rf /usr/local/civicmesh && userdel civicmesh && rm
/etc/systemd/system/civicmesh-* && rm /etc/hostapd/hostapd.conf` (and
the other rendered files). Self-contained.

---

## FILESYSTEM LAYOUT (DEV)

```
~/code/CivicMesh/                             # or wherever the developer cloned it
├── .venv/                                    # uv-managed; gitignored
├── pyproject.toml
├── uv.lock                                   # COMMITTED
├── config.toml                               # gitignored; per-machine, written by `configure`
├── config.toml.example                       # COMMITTED; reference with inline comments
├── civic_mesh.db                             # gitignored
├── logs/                                     # gitignored
├── docs/
│   └── civicmesh-tool.md                     # this document
├── web_server.py
├── mesh_bot.py
├── civicmesh.py                              # was admin.py
├── ...
└── static/
```

**`.gitignore` must include:** `.venv/`, `config.toml`, `civic_mesh.db`,
`logs/`, `__pycache__/`, `*.pyc`.

**Cleanup task before merging this doc:** remove `config.toml` from git
tracking and replace with `config.toml.example`. See "Repository
cleanup" below.

---

## TOOLCHAIN: uv

CivicMesh uses [uv](https://docs.astral.sh/uv/) (Astral) as the
dependency and venv manager. It replaces `pip`, `venv`, and `pyenv` in
the workflow.

Key files:

- **`pyproject.toml`** — dependency declarations, project metadata,
  console scripts. Hand-edited.
- **`uv.lock`** — fully resolved dependency graph with exact versions
  and wheel hashes. Generated by uv. **Committed to git.**

Key commands (developer):

```bash
uv sync                  # build .venv to match pyproject.toml + uv.lock
uv sync --frozen         # build .venv strictly from uv.lock; fail if lock is stale
uv add requests          # add a runtime dependency
uv add --dev pytest      # add a dev-only dependency
uv remove requests       # remove a dependency
uv lock                  # regenerate uv.lock from pyproject.toml
uv lock --check          # exit non-zero if lock is out of date
uv run civicmesh stats   # run civicmesh inside the project venv (no activation needed)
uv run python -m unittest  # run any python command inside the venv
```

Production uses `uv sync --frozen` only. Bootstrap installs uv (via the
standalone installer) into the `civicmesh` user's home, then runs
`uv sync --frozen` once. Promote re-runs `uv sync --frozen` after each
source update.

`uv.lock` should be regenerated whenever a dependency in
`pyproject.toml` changes (`uv add` does this automatically). A stale
lock will cause `uv sync --frozen` to fail loudly in promote/bootstrap,
which is the desired behavior.

---

## CONFIGURATION FILE

`config.toml` is the single source of truth for all runtime config.
Hand-editable for Tier 2 fields (see `configure` for the
Tier 1 / Tier 2 split).

### Sections

```toml
[node]
site_name = "CivicMesh"           # human-readable hub label
callsign = "civic1"               # short on-wire identity, <=9 chars

[network]                            # NEW: addressing/DHCP/wireless-iface concerns
ip = "10.0.0.1"
subnet_cidr = "10.0.0.0/24"
iface = "wlan0"
country_code = "US"
dhcp_range_start = "10.0.0.10"
dhcp_range_end = "10.0.0.250"
dhcp_lease = "15m"

[ap]                                 # NEW: wireless-only concerns
ssid = "CivicMesh-Fremont"
channel = 6                          # 1, 6, or 11

[radio]                              # MeshCore radio (USB-serial)
serial_port = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_..."
freq_mhz = 910.525
bw_khz = 62.5
sf = 7
cr = 5

[channels]
names = ["#civicmesh", "#puget-sound"]

[local]
names = ["#local"]

[web]                                # CHANGED: portal_host removed (derived from network.ip)
port = 8080
portal_aliases = ["civicmesh.internal"]    # likely renames to captive_portal_aliases in a future ticket

[limits]
posts_per_hour = 10
message_max_chars = 100
name_max_chars = 12
name_pattern = "^[A-Za-z0-9_-]+$"
outbox_max_retries = 3
outbox_max_delay_sec = 10
outbox_idle_reset_sec = 60
outbox_echo_wait_sec = 8
retention_bytes_per_channel = 10737418240   # 10 GiB
hub_docs_retention_count = 3            # hub-docs releases retained; oldest pruned on install
global_egress_per_hour = 200            # sliding-hour mesh egress ceiling; sized below ~360/hr radio capacity
outbox_max_depth = 60                   # queued-row cap; over-cap POSTs get 429 with retry_after_sec=60

[logging]
log_level = "INFO"
log_dir = "logs"                     # relative to project_root in dev, /usr/local/civicmesh/var/logs in prod
enable_security_log = true

[debug]
allow_eth0 = false                   # application-only flag; not consulted by `apply`

[recovery]
# Defaults are sane; only edit if you know the radio recovery state machine.
liveness_interval_sec = 30.0
liveness_timeout_sec = 5.0
liveness_consecutive_threshold = 3
outbox_consecutive_threshold = 3
verify_timeout_sec = 5.0
post_rts_settle_sec = 5.0
rts_pulse_width_sec = 0.1
flapping_window_sec = 3600
flapping_max_recoveries = 6
backoff_base_sec = 60.0
backoff_cap_sec = 3600.0
```

### Section ownership

| Section | Layer | Consumed by |
|---|---|---|
| `[node]` | identity | web, mesh, sessions |
| `[network]` | L2/L3 addressing | `apply` only |
| `[ap]` | wireless | `apply` only |
| `[radio]` | radio (USB-serial) | mesh_bot only |
| `[channels]`, `[local]` | application | web, mesh |
| `[web]` | application (HTTP) | web_server only |
| `[limits]` | policy | web |
| `[logging]` | infrastructure | all |
| `[debug]` | dev-only | web (only) |
| `[recovery]` | recovery state machine | mesh_bot only |

Within `[limits]`, `hub_docs_retention_count` is the exception:
consumed by `civicmesh install-hub-docs`, not by the running web
or mesh services.

`[network]` and `[ap]` are split because: `[network]` is "what address
space, what interface, what DHCP" — pure L2/L3 facts, consumed only by
`apply` when rendering hostapd/dnsmasq/nftables/networkd. `[ap]` is
"what wireless network do we broadcast" — SSID and channel. The split
keeps `[ap]` useful even if a future deployment changes addressing
without touching wireless settings (or vice versa).

`[debug].allow_eth0` is **not** consulted by `apply`. It's a runtime
flag the web server reads to decide whether to bypass MAC validation
for traffic arriving on `eth0`. The firewall (rendered by `apply`)
does not change based on this flag — eth0 is allowed at the network
layer regardless. This means an operator can flip `allow_eth0` without
fearing that the next `apply` will lock them out.

### Cross-field validators (run by `config validate` and `apply`)

- `network.iface` is a valid Linux interface name pattern
- `ap.channel` ∈ {1, 6, 11}
- `ap.ssid` length 1–32
- `network.country_code` matches `[A-Z]{2}`
- `network.ip` is inside `network.subnet_cidr`
- `network.dhcp_range_start` and `network.dhcp_range_end` are inside `network.subnet_cidr`
- `network.dhcp_range_start` ≤ `network.dhcp_range_end`
- `network.ip` is *not* inside `[dhcp_range_start, dhcp_range_end]`
- `radio.serial_port` exists if path starts with `/dev/serial/by-id/` (warning, not error — useful when generating config offline)

### Computed values (not in config.toml)

- `web.portal_host` = `network.ip` (always; web_server reads
  `network.ip` directly)

---

## SYSTEM FILES MANAGED BY APPLY

The mapping from config fields to rendered files. Use this as a
checklist: if you change a field, the listed files regenerate and the
listed services restart.

| Config field | Files affected | Services restarted |
|---|---|---|
| `ap.ssid`, `ap.channel`, `network.iface`, `network.country_code` | `hostapd.conf`, `default/hostapd` | `hostapd` |
| `network.ip`, `network.subnet_cidr`, `network.dhcp_range_*`, `network.dhcp_lease` | `dnsmasq.d/civicmesh.conf` | `dnsmasq` |
| `network.iface`, `network.ip` | `networkd 20-<iface>-ap.network` | `systemd-networkd` |
| `network.iface` | `NetworkManager/conf.d/99-unmanaged-<iface>.conf`, `sysctl.d/90-civicmesh-disable-ipv6.conf` | `NetworkManager` (reload), `sysctl --system` |
| `network.iface`, `web.port` | `nftables.conf` | `nft -f` (atomic ruleset swap; no service restart) |

Application-layer fields (`[node]`, `[radio]`, `[channels]`, `[local]`,
`[web].port`, `[web].portal_aliases`, `[limits]`, `[logging]`,
`[debug]`, `[recovery]`) do **not** trigger system file regeneration.
They are consumed by the running Python services. After editing those
sections, the operator runs:

```bash
sudo systemctl restart civicmesh-web civicmesh-mesh
```

---

## COMMANDS

### bootstrap                                                            [NEW]

```
curl -sSL https://example.com/civicmesh-bootstrap.sh | sudo bash
```

This is **not** a `civicmesh` subcommand — by definition, `civicmesh`
isn't installed yet when bootstrap runs. It's a standalone shell script
delivered via curl. Once bootstrap completes, the operator uses
`civicmesh` for everything else.

Bootstrap is essentially a from-scratch promote: it lays down the
`/usr/local/civicmesh/` tree and produces a working venv from a known
git source.

**What bootstrap does, in order:**

1. Verify running as root.
2. Verify on a supported OS (Debian/Raspberry Pi OS).
3. `apt-get install` system dependencies: `git`, `curl`, `python3`,
   `python3-venv`, `hostapd`, `dnsmasq`, `nftables`, `rfkill`,
   `network-manager`. **Does not** install `apt-offline` (the
   retired `setup_ap.sh` used to; CIV-64 dropped it).
4. Disable conflicting services if present: `dhcpcd`,
   `systemd-resolved` stub listener. (Bootstrap deliberately leaves
   `wpa_supplicant` running so the SSH session this script is
   executing over — typically over WiFi on a Pi imaged with the Pi
   Imager WiFi flow — survives. `apply` is what disables
   `wpa_supplicant` for the next boot, just before the operator
   reboots into AP mode.)
5. Mask `systemd-rfkill.service` and `.socket`. Install
   `/etc/systemd/system/rfkill-unblock-wifi.service`. Enable it.
6. `useradd -r -m -d /usr/local/civicmesh -s /bin/bash civicmesh`.
7. As `civicmesh`: install uv via the standalone installer
   (`curl -LsSf https://astral.sh/uv/install.sh | sh`), which puts the
   uv binary in `/usr/local/civicmesh/.local/bin/`.
8. As `civicmesh`: `git clone <repo-url> /usr/local/civicmesh/app`.
9. As `civicmesh`: `cd /usr/local/civicmesh/app && uv sync --frozen`.
10. Create `ln -s /usr/local/civicmesh/app/.venv/bin/civicmesh
    /usr/local/bin/civicmesh` (and similar for `civicmesh-web`,
    `civicmesh-mesh`).
11. `mkdir /usr/local/civicmesh/etc /usr/local/civicmesh/var
    /usr/local/civicmesh/var/logs`, `chown civicmesh:civicmesh` on
    each.
12. Print next-steps banner:

    ```
    Bootstrap complete.

    Next:
      sudo -u civicmesh civicmesh configure   # write the config interactively
      sudo civicmesh apply                    # render system files, start services
    ```

**What bootstrap does NOT do:**

- Does not run `configure` (that requires operator input).
- Does not run `apply` (that requires `configure` first).
- Does not enable systemd units for `civicmesh-web`/`civicmesh-mesh`
  (that's `apply`'s job; the units don't exist yet).
- Does not stop or disable `wpa_supplicant`. Disabling here would kill
  the SSH session bootstrap is running over (on a headless Pi imaged
  with the Pi Imager WiFi flow, that session is held up by
  `wpa_supplicant`). The disable happens in `apply`, where the operator
  reboot is the cutover.

**Failure modes:**

- Any `apt-get` failure: abort, no rollback. Operator can re-run
  bootstrap (it's idempotent for already-installed packages).
- `git clone` failure (e.g., no network): abort. Partial filesystem
  state is left for the operator to clean up manually; we do not
  attempt rollback.

---

### configure                                                            [NEW]

```
DEV:   uv run civicmesh configure
PROD:  sudo -u civicmesh civicmesh configure
```

Walk the operator through Tier 1 (per-deployment) fields, then write
`config.toml`. If the file already exists, current values are shown as
defaults; pressing Enter keeps the current value.

**Output path is determined by mode**, not by `--config`:

- DEV: writes `<project_root>/config.toml`
- PROD: writes `/usr/local/civicmesh/etc/config.toml`

`--config PATH` is accepted but, per the refusal rules, must point
inside the mode's allowed tree.

**Tier 1 fields (prompted):**

| Field | Default | Notes |
|---|---|---|
| `node.site_name` | _(required)_ | Human-readable name of the physical hub/site this node serves. Portal masthead, captive-portal `<title>`s, stamped into `session.location` for new sessions. |
| `node.callsign` | _(required)_ | Short on-wire identity, 1-9 chars `[A-Za-z0-9_-]`, lowercased on load. Firmware sets this as the SenderName prefix on every channel message. |
| `channels.names` | _(none)_ | Subprompt: add / remove / clear / done. |
| `radio.serial_port` | auto-detected if exactly one CP2102 USB-serial device exists | Otherwise prompts with detected candidates. |
| `ap.ssid` | `CivicMesh-Messages` | 1–32 chars. |
| `ap.channel` | `6` | Must be `1`, `6`, or `11`. |
| `network.iface` | `wlan0` (or sole `wlan*` if exactly one) | Validated against `ip link`. |
| `network.country_code` | `US` | Two-letter ISO 3166. |
| `debug.allow_eth0` | `false` | Asked as: "Is this a development machine reachable over wired ethernet?" |

**Tier 2 fields (not prompted; edit `config.toml` by hand):**

Everything in `[radio]` except `serial_port`; everything in `[limits]`,
`[recovery]`, `[logging]`; the IP/DHCP fields under `[network]`;
everything in `[web]`.

**Auto-detection rules:**

- `radio.serial_port`: scan `/dev/serial/by-id/` for entries matching
  `*Silicon_Labs*CP2102*` or `*USB_to_UART*`.
  - 1 match: use it without prompting; show what was detected.
  - 0 matches: prompt with explanation.
  - >1 match: list them, prompt for selection.
- `network.iface`: scan `ip -j link show` for `wlan*` interfaces.
  - 1 match: default to it.
  - 0 or >1: default to `wlan0` and prompt.

**Output:**

```
Wrote /usr/local/civicmesh/etc/config.toml

Next: sudo civicmesh apply
```

(In dev mode, the next-step hint says `civicmesh apply --dry-run` for
testing or `civicmesh promote --from .` for deployment.)

**Implementation notes:**

- Plain `input()`-based prompts; no TUI, no third-party prompt
  library. Keeps the dep tree minimal and works over any terminal
  (USB serial console included).
- Validate per-field as you go; don't accept bad input and re-prompt
  at the end.
- Channel name list editing is a sub-loop:
  `[a]dd / [r]emove / [c]lear / [d]one`.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Wrote config.toml successfully. |
| 1 | I/O or permission error. |
| 2 | Validation failed. |
| 3 | User aborted (Ctrl-C, or "no" at final confirmation). |
| 10 | Wrong-mode, or invoked as root in prod (see below). |

**Prod-root refusal:** `configure` refuses to run as root in prod (exit
10). Running as root would write `/usr/local/civicmesh/etc/config.toml`
as `root:root` mode 0600, which the `civicmesh` user cannot read; the
services would then fail at next boot with a confusing permission error
far from the cause. The fix is to invoke as the `civicmesh` user:
`sudo -u civicmesh civicmesh configure`.

---

### apply                                                                [NEW]

```
PROD:  sudo civicmesh apply [--no-restart]
DEV:   uv run civicmesh apply --dry-run
PROD:  sudo civicmesh apply --dry-run    (also valid)
```

Render the system files listed under "System files managed by apply"
from `config.toml`, validate them, write them, and stage AP mode for
the next boot. Idempotent.

System-stack services (hostapd, dnsmasq, nftables, networkd,
NetworkManager, sysctl) are deliberately **not** restarted in-place.
The cutover is the operator-issued reboot. This avoids the
headless-WiFi trapdoor: starting hostapd while `wpa_supplicant` is
still up — or before nftables is loaded — would drop an SSH session
the operator is running over wlan0.

**Phases (in order):**

1. **Read & validate** `config.toml`. Refuse on validation errors.
2. **Render to memory**: produce full bytes of every managed file.
3. **Diff against on-disk**: byte-compare each rendered file. Build
   the set of files that changed.
4. **Pre-flight syntax validation** (before any write):
   - `dnsmasq.d/civicmesh.conf` → `dnsmasq --test --conf-file=<tmp>`
   - `nftables.conf` → `nft -c -f <tmp>`
   - `network.iface` → must exist under `/sys/class/net/`

   (hostapd has no offline syntax-check mode — `hostapd -t` is a
   debug-timestamp flag, and a real check would try to bind wlan0.
   Renderer goldens cover hostapd config correctness in tests instead.)

   Validators run against tempfile copies of the rendered bytes — never
   the target paths in `/etc/` — so a failed validation cannot leave a
   half-written config behind. On any failure, exit 6 with no
   filesystem or systemd state changes.
5. **Write changed files atomically**: tmpfile in same directory,
   `os.fchmod` to correct mode, `os.replace` to swap. No partial
   states.
6. **Stage for boot** (always; idempotent):

   | Step | Action |
   |---|---|
   | 1 | `systemctl daemon-reload` |
   | 2 | `systemctl unmask hostapd.service dnsmasq.service` *(satisfies Debian package contract — see below)* |
   | 3 | `systemctl enable hostapd dnsmasq nftables rfkill-unblock-wifi` |
   | 4 | `systemctl disable wpa_supplicant.service` |
   | 5 | `systemctl enable civicmesh-web civicmesh-mesh` |
   | 6 | `systemctl restart civicmesh-web civicmesh-mesh` *(only if their unit files changed)* |

   The `unmask` step exists because the Debian `hostapd` and `dnsmasq`
   package postinst masks the unit at install time with the rationale
   "don't auto-start until the operator has rendered a config." apply
   is the operator-driven step that renders those configs (the
   immediately-preceding write phase wrote them), so unmasking belongs
   here — not in bootstrap. `systemctl unmask` is idempotent on
   already-unmasked units, so this is safe on every re-run.

   System-stack config changes (hostapd / dnsmasq / nftables /
   networkd / NetworkManager / sysctl) require a reboot to take
   effect. The cutover banner tells the operator that.
7. **Print cutover banner**: explains the system is staged for AP
   mode, that the current SSH session is fine because the cutover
   only happens on `sudo reboot`, and warns that the SSH session
   will end on reboot if it's running over WiFi.

**Options:**

| Flag | Effect |
|---|---|
| `--dry-run` | Phases 1–3 only. Print unified diff for each changed file, list services that would restart, exit 0 without writing. **Does not require root.** Works in both modes. Pre-flight validation does not run in dry-run (it would need root to read /etc/ and the binaries are validated for real on `apply`). |
| `--no-restart` | Phases 1–5 only. Files written, but no `systemctl daemon-reload`, `enable`, `disable`, or app restart. The cutover banner is suppressed. Use when you want to inspect the rendered files before staging service state changes; you must run a normal `apply` (or the equivalent systemctl commands) before rebooting, otherwise the system reboots into client mode again. |

**iface change handling:**

The networkd and NetworkManager filenames embed `network.iface`. If
`network.iface` changes between runs (e.g., `wlan0` → `wlan1` for an
external USB radio), the old files become orphans. **For this round,
`apply` does not clean up orphans.** Document the limitation; punt the
tracked-files registry until external USB wifi is a real requirement.

Workaround for an iface change today: run `sudo civicmesh apply`, then
manually `rm` the stale `99-unmanaged-wlan0.conf` and
`20-wlan0-ap.network` if any.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Apply succeeded (with or without changes). |
| 1 | Not running as root (real apply only; `--dry-run` doesn't need root). |
| 2 | Config file missing or unparseable. |
| 3 | Config validation failed. |
| 4 | A file write failed mid-apply. **No automatic rollback.** |
| 5 | A staging step failed (`daemon-reload`/`enable`/`disable`/app restart) after files were written. |
| 6 | Pre-flight syntax validation failed. **No filesystem or systemd state changes.** |
| 10 | Wrong-mode (see refusal rules). |

**Failure handling:**

- Mid-apply write failure (exit 4): already-written files are not
  reverted. Summary lists what was written. Operator fixes root cause
  and re-runs.
- Post-write service restart failure (exit 5): files kept as-written.
  Inspect with `journalctl -u <service> -e`. Re-run `apply` (no-op for
  unchanged files) once underlying issue is fixed.
- Always prefer `--dry-run` first on a live hub.

**Testing the unmask path without reimaging:**

To re-exercise the fresh-postinst code path (e.g. after touching the
`systemctl unmask` step) without flashing a new SD card:

```bash
# Reset to fresh-postinst state to re-test the unmask path:
sudo systemctl unmask hostapd dnsmasq
sudo apt-get install --reinstall -y hostapd dnsmasq
# Verify postinst re-masked: should show /dev/null symlink.
ls -la /etc/systemd/system/hostapd.service
# Now exercise apply:
sudo civicmesh apply
```

The reinstall step is what triggers the package postinst to re-mask
the unit; without it, `apt-get install` would no-op since the package
is already at the latest version.

---

### When to promote, apply, and reboot                                   [NEW]

Decision rule for an operator who just made a change in dev. Use
this to answer "I just promoted; do I also need to apply? Do I
need to reboot?"

| Change type | promote | apply | reboot |
|---|:---:|:---:|:---:|
| Pure code change (`mesh_bot.py`, `web_server.py`, `civicmesh.py`, `database.py`, etc.) | yes (with `--restart`, or restart manually after) | no | no |
| `civicmesh-web.service` / `civicmesh-mesh.service` unit-file change | yes (no `--restart` needed — `apply` restarts these in-place) | yes | no |
| `apply/renderers.py` change | yes (no `--restart` needed — `apply` restarts in-place if a unit file changed) | yes | depends — see below |
| `config.toml` schema change (new field consumed by `config.py`) | yes — but run `configure` and `apply` *before* restarting; otherwise the new code will reject the existing config | yes | depends — see below |
| Rendered output changes for `hostapd.conf`, `dnsmasq.d/civicmesh.conf`, `nftables.conf`, `99-unmanaged-<iface>.conf`, `20-<iface>-ap.network`, or `90-civicmesh-disable-ipv6.conf` | yes (no `--restart` needed — reboot is the cutover) | yes | yes |

**The "depends" rows:** the reboot requirement is determined by
which *rendered* file changes, not by which source file you
edited. A renderer or schema change that produces a new
`hostapd.conf`, `dnsmasq.d/civicmesh.conf`, `nftables.conf`, or
any networkd / NetworkManager / sysctl file requires a reboot; a
renderer change that only touches `civicmesh-*.service` does not.
Confirm before deciding:

```bash
sudo civicmesh apply --dry-run
```

If the change list includes any system-stack file (hostapd /
dnsmasq / nftables / networkd / NetworkManager / sysctl), reboot.
If it only includes `civicmesh-*.service`, no reboot.

**Why reboot at all.** `apply` deliberately doesn't restart
hostapd / dnsmasq / nftables / networkd / NetworkManager / sysctl
in-place; it only stages them via `systemctl enable` / `disable`
and a `daemon-reload`. The operator-issued `sudo reboot` is the
cutover. Restarting in-place would risk the headless-WiFi
trapdoor described in the `apply` section above.

**Escape hatch — nftables only.** If the *only* changed file is
`/etc/nftables.conf`, `sudo nft -f /etc/nftables.conf` performs an
atomic ruleset swap with no service restart and no reboot. (This
is the "Services restarted" entry for nftables in *System files
managed by apply* above.) All other system-stack changes still
require a reboot.

---

### promote                                                              [NEW]

```
DEV ONLY:  uv run civicmesh promote --from <dev-tree> [--dry-run] [--restart]
```

Push the dev tree's `main` branch to the prod tree and rebuild prod's
venv. **Does not restart `civicmesh-web` / `civicmesh-mesh` by
default** — the running services keep serving the old code until the
operator restarts them. Pass `--restart` to restart automatically.

> **Behavior change.** Earlier versions of promote always restarted
> services on success. The default flipped because promote can't tell
> whether the new code is config-compatible: a schema-breaking change
> would crash-loop the units the moment systemd restarted them, and
> "is now a good moment for a brief outage" is operator context that
> promote doesn't have.

`<dev-tree>` defaults to `.` (current directory). `--from` is required
in argument form for explicitness; if omitted, the cwd must be a dev
tree.

**Strict pre-flight checks (refuses on any failure):**

1. `<dev-tree>` is a git repo with `pyproject.toml` declaring
   `name = "civicmesh"`.
2. `main` branch exists locally.
3. HEAD is on `main` (refuse if on a feature branch — even if it looks
   "obviously" right).
4. Working tree is clean: no untracked files in tracked directories,
   no unstaged changes, no staged-but-uncommitted changes.
5. `uv.lock` is up to date with `pyproject.toml` (run
   `uv lock --check`; fail if changes would be made).
6. The prod tree at `/usr/local/civicmesh/app/` exists (bootstrap has
   run on this machine).

There is no `--force`. If you need to override a check, fix the
underlying state.

**What promote does:**

1. Print a summary: dev's `main` HEAD commit, prod's current commit
   (if reachable in dev's git log), the file diff between them.
2. Confirm with operator unless `--dry-run`.
3. `git archive main | sudo tar -x -C /usr/local/civicmesh/app/`
   — extracts only files tracked at `main` HEAD. `.venv/`, untracked
   files, gitignored files do not transfer.
4. `sudo -u civicmesh sh -c 'cd /usr/local/civicmesh/app && uv sync --frozen'`
   — rebuilds prod venv with prod's absolute paths in launcher
   shebangs.
5. **Only if `--restart` was passed:**
   `sudo systemctl restart civicmesh-web civicmesh-mesh`.
6. Print summary of what changed and service-restart status. When
   `--restart` was *not* passed, the summary names the literal
   `sudo systemctl restart civicmesh-web civicmesh-mesh` command for
   the operator to run when ready, and reminds them to run
   `sudo -u civicmesh civicmesh configure` first if the PR changed
   the config schema.

**What promote does NOT do:**

- Does not import or call any code from the prod tree. Operates
  entirely via shell-outs (`git`, `tar`, `sudo`, `uv`, `systemctl`).
  This means a broken prod tree can be repaired by re-running
  promote; promote logic itself isn't dependent on prod.
- Does not touch `/usr/local/civicmesh/etc/config.toml` or
  `/usr/local/civicmesh/var/`. Config and database persist across
  promotes.
- Does not run `apply`. If config schema changed (new fields, etc.),
  operator must run `sudo civicmesh apply` separately.

**Why git archive instead of rsync:**

`git archive main` outputs only what's committed at `main` HEAD. A
stray `notes.txt`, `*.pyc`, or `__pycache__/` in the working tree
cannot leak into prod, even if the working-tree-clean check has a
bug. This is belt-and-suspenders safety.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Promote succeeded. |
| 1 | Pre-flight check failed. Message names the failing check. |
| 2 | git/tar/uv/systemctl shell-out failed. Message names the step. |
| 3 | User aborted at confirmation. |
| 10 | Wrong-mode (running from prod tree). |

---

### install-hub-docs                                                     [NEW]

```
PROD:  sudo -u civicmesh civicmesh install-hub-docs <zip> [--dry-run] [--config PATH]
DEV:   uv run civicmesh install-hub-docs <zip> [--dry-run] [--config PATH]
```

Install a hub-docs release zip onto the node. Extracts to a staged
incoming directory, applies the §3 install-time validation rules,
promotes to a release directory, and atomically swaps the
`<var>/hub-docs` symlink to point at the new release. See
`docs/hub-reference-library.md` § INSTALL PROCESS for the
step-by-step procedure, the validation rules in § THE CONTRACT:
`index.json` SCHEMA, and the pruning semantics (including the
rollback-self-prune protection).

The atomic swap means an in-flight HTTP request reading from the
old release survives the install: the kernel keeps the old
release's inodes alive until the request closes the file
descriptor. No service restart is needed.

**Prod-root refusal:** `install-hub-docs` refuses to run as root in
prod (exit 10). It writes under `<var>/hub-docs.releases/`, owned
`civicmesh:civicmesh`. Running as root would create root-owned
release directories the service user cannot later touch (mirror of
the failure mode `configure` guards against). The fix is to invoke
as the `civicmesh` user:
`sudo -u civicmesh civicmesh install-hub-docs ...`.

**Options:**

| Flag | Effect |
|---|---|
| `--dry-run` | Run the install through validation only — extract, peek `index.json`, validate per §3, then `rm -rf` the incoming directory. The symlink is not touched, no release directory persists. Stdout names the would-be release_id and document count. Useful to verify a zip on a node before committing. |

**Exit codes** (copy of § INSTALL PROCESS / CLI conventions):

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | I/O error (zip unreadable, disk full during extract, permission error, `--config` load failed). |
| 2 | Argument is not a zip / does not parse, or zip member resolves outside the staging dir (zip-slip). |
| 3 | §3 validation failed on extracted contents. |
| 4 | `<release_id>/` already populated (a prior install used this exact release_id). |
| 10 | Wrong-mode, or invoked as root in prod. |

**Stdout format** (copy of § INSTALL PROCESS / CLI conventions):

- success: `installed release_id=<id> previous=<id_or_none> pruned=<N>`.
  On first install, `previous=none`.
- `--dry-run`: `dry_run release_id=<id> docs=<N>`.
- errors go to stderr, prefixed `civicmesh install-hub-docs: `.

**Examples:**

```bash
# Build on dev, ship to a node, install:
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ --out out/
ZIP=out/hub-docs-20260506T143200Z.zip
scp "$ZIP" user@hub-fremont:/tmp/
ssh user@hub-fremont \
    "sudo -u civicmesh civicmesh install-hub-docs /tmp/$(basename $ZIP)"
```

```bash
# Verify a zip end-to-end without committing:
sudo -u civicmesh civicmesh install-hub-docs /tmp/hub-docs-*.zip --dry-run
```

```bash
# Multi-node fan-out from a build host (per § RELEASE PROCEDURE
# in docs/hub-reference-library.md):
ZIP=out/hub-docs-20260506T143200Z.zip
for node in toorcamp-01 toorcamp-02 toorcamp-03; do
  scp "$ZIP" user@$node:/tmp/
  ssh user@$node \
      "sudo -u civicmesh civicmesh install-hub-docs /tmp/$(basename $ZIP)"
done
```

---

### rollback-hub-docs                                                    [NEW]

```
PROD:  sudo -u civicmesh civicmesh rollback-hub-docs [--to <release_id>] [--config PATH]
DEV:   uv run civicmesh rollback-hub-docs [--to <release_id>] [--config PATH]
```

Roll the `<var>/hub-docs` symlink back to a prior release directory
under `<var>/hub-docs.releases/`. No re-extract, no validation — the
target was already extracted and validated when it was originally
installed. Same atomic symlink-swap mechanism as install. See
`docs/hub-reference-library.md` § INSTALL PROCESS / Rollback for
the full semantics.

With no flag, rolls back to the lex-greatest `release_id` that is
*not* the current symlink target. With `--to <release_id>`, rolls
back to the named release. Rollback to the *current* target is a
documented no-op: exit 0, stdout includes `noop=true`, no
filesystem change.

**Prod-root refusal:** `rollback-hub-docs` refuses to run as root in
prod (exit 10). It writes the symlink under `<var>/`, owned
`civicmesh:civicmesh`. Same rationale as `install-hub-docs`. The
fix is `sudo -u civicmesh civicmesh rollback-hub-docs ...`.

**Options:**

| Flag | Effect |
|---|---|
| `--to <release_id>` | Roll back to the named release. The id must exist as a directory under `<var>/hub-docs.releases/`. If `--to` is omitted, picks the lex-greatest non-current release. |

**Exit codes** (copy of § INSTALL PROCESS / CLI conventions):

| Code | Meaning |
|---|---|
| 0 | Success, or no-op (`--to` matches current target). |
| 1 | I/O error (permission, symlink read failure). |
| 4 | Rollback target missing (`--to <id>` not present), or only one release installed (no-flag form has nothing to roll back to). |
| 10 | Wrong-mode, or invoked as root in prod. |

(Codes 2 and 3 from the install table do not apply: rollback never
opens a zip and never validates contents.)

**Stdout format** (copy of § INSTALL PROCESS / CLI conventions):

- success: `rolled_back release_id=<id> previous=<id>`.
- no-op (`--to` matches current): `rolled_back release_id=<id> previous=<id> noop=true`.
- errors go to stderr, prefixed `civicmesh rollback-hub-docs: `.

**Examples:**

```bash
# Roll back to the most recent prior release (typical "undo last install"):
ssh user@hub-fremont \
    "sudo -u civicmesh civicmesh rollback-hub-docs"
```

```bash
# Roll back to a specific release; the id is the directory name
# under /usr/local/civicmesh/var/hub-docs.releases/:
ssh user@hub-fremont \
    "sudo -u civicmesh civicmesh rollback-hub-docs --to 20260315T093015Z"
```

```bash
# List available rollback targets on a node:
ssh user@hub-fremont \
    "ls /usr/local/civicmesh/var/hub-docs.releases/"
```

---

### config show                                                          [NEW]

```
civicmesh config show [--config PATH] [--format {toml,json}]
```

Print the parsed effective config (after defaults are filled in).
Useful for "what does the app actually see?" when the toml is
incomplete.

Pure file → object → serializer. Does not contact services or DB.

---

### config validate                                                      [NEW]

```
civicmesh config validate [--config PATH]
```

Parse `config.toml`, run all validators that `apply` would run, report
errors. Exits 0 on valid config, 1 on invalid. Useful in CI or
pre-commit hooks against checked-in `config.toml.example`.

---

### stats                                                                [IMPL]

```
civicmesh stats
```

Print one-line counters: total messages, total sessions, queued
outbox, total votes.

```
messages=12345 sessions=67 outbox_pending=3 votes=89
```

**Drift note:** the UI's `/api/stats` returns ~30 fields. CLI exposes
4. Closing the gap is straightforward (call `compute_stats` and
pretty-print) but is not part of this round.

---

### cleanup                                                              [IMPL]

```
civicmesh cleanup [--channel NAME]
```

Run retention cleanup. Without `--channel`, cleans every configured
channel (`local.names` + `channels.names`). Uses
`limits.retention_bytes_per_channel` (default 10 GiB).

Output: `deleted=N`.

---

### messages recent                                                      [IMPL]

```
civicmesh messages recent [--channel NAME] [--source {mesh,wifi}]
                          [--session ID] [--limit N]
```

List recent messages. Default limit 20. Filters combine (AND).
Fixed-width text table. Sender and content shown via `repr()` to make
non-printable bytes visible.

Columns: `ID TS CH SRC ST RT SESSION SENDER CONTENT`.

---

### outbox list / outbox cancel / outbox clear                           [IMPL]

```
civicmesh outbox list   [--channel NAME] [--limit N]
civicmesh outbox cancel <outbox_id> [--skip_confirmation]
civicmesh outbox clear  [--skip_confirmation]
```

`list` shows pending outbox messages. `cancel` removes one (with
confirmation prompt unless `--skip_confirmation`). `clear` cancels all
pending (with confirmation).

Outputs: `canceled=1 id=<id>` / `cleared=N`.

---

### sessions list / sessions show / sessions reset                       [IMPL]

```
civicmesh sessions list  [--limit N]
civicmesh sessions show  <session_id>
civicmesh sessions reset <session_id>
```

`list` orders by `last_post_ts` desc; columns `SESSION LAST NAME LOC
MAC POSTS`. `show` prints all session fields, one `key=value` per
line. `reset` zeros `post_count_hour`.

---

### pin / unpin                                                          [IMPL]

```
civicmesh pin   <message_id> [--order N]
civicmesh unpin <message_id>
```

Pin a message (survives retention cleanup). `--order` sets pin
priority (lower = higher in list); default is next available slot.

**Drift note:** UI doesn't surface pinned messages explicitly yet.
Schema and admin command both work; rendering is the gap.

---

## EXAMPLES

### First-time setup on a fresh Pi

```bash
# As any user with sudo:
curl -sSL https://example.com/civicmesh-bootstrap.sh | sudo bash

sudo -u civicmesh civicmesh configure
sudo civicmesh apply

# Optional: reboot to verify everything comes up cleanly
sudo reboot
```

### Hacking on a deployed node (Toorcamp pattern)

```bash
# Stop production temporarily so the radio is yours:
sudo systemctl stop civicmesh-web civicmesh-mesh

# Hack in your dev tree, against your dev config:
cd ~/code/CivicMesh
uv run civicmesh-web --config config.toml      # your dev config, your dev db, your dev port
# (Run mesh_bot in another terminal: uv run civicmesh-mesh --config config.toml)

# Once you're happy, commit and promote:
git checkout main && git merge fix-rfkill
uv run civicmesh promote --from . --restart

# Production resumes (--restart bounces the units; without that flag,
# promote ships the code but leaves the running services on the old
# version until you restart them yourself).
```

### Updating a Tier 1 field (e.g., adding a channel)

```bash
# On the prod box:
sudo -u civicmesh civicmesh configure          # walk through, change channels.names
sudo systemctl restart civicmesh-mesh          # [channels] is application-layer; no apply needed
```

### Updating a Tier 2 field (e.g., relaxing rate limit)

```bash
sudo -u civicmesh $EDITOR /usr/local/civicmesh/etc/config.toml
# (change limits.posts_per_hour from 10 to 30)
sudo systemctl restart civicmesh-web
```

### Changing the WiFi channel before a busy event

```bash
sudo -u civicmesh civicmesh configure          # change ap.channel
sudo civicmesh apply --dry-run                 # eyeball the diff
sudo civicmesh apply                           # stage; SSH session survives
sudo reboot                                    # cutover (ap.channel → hostapd.conf → reboot)
```

### Manual moderation

```bash
civicmesh outbox list --channel "#fremont"
civicmesh outbox cancel 4271
```

### Verifying everything before promote

```bash
cd ~/code/CivicMesh
uv run civicmesh config validate               # static check
uv run civicmesh apply --dry-run               # would-be-rendered diff vs prod /etc
uv run civicmesh promote --from . --dry-run    # what promote would do
uv run civicmesh promote --from .              # commit (services keep running old code)
# Then, when you're ready to cut over:
sudo systemctl restart civicmesh-web civicmesh-mesh
# (or pass --restart to the promote above to combine the two steps)
```

### Shipping new hub-docs to a node

```bash
# On the dev box: refresh the curated PDFs in content/hub-docs/,
# edit manifest.toml if needed, then build the zip.
cd ~/code/CivicMesh
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ --validate    # quick parseability check
uv run python scripts/build_hub_docs.py \
    --source content/hub-docs/ --out out/

# Ship and install. --dry-run first if you want to validate the zip
# on the node before committing the symlink swap.
ZIP=$(ls -t out/hub-docs-*.zip | head -1)
scp "$ZIP" user@hub-fremont:/tmp/
ssh user@hub-fremont \
    "sudo -u civicmesh civicmesh install-hub-docs /tmp/$(basename $ZIP)"

# If the new release surfaces a problem, roll back without re-shipping:
ssh user@hub-fremont \
    "sudo -u civicmesh civicmesh rollback-hub-docs"
```

See `docs/hub-reference-library.md` § RELEASE PROCEDURE for the
multi-node fan-out variant.

---

## TROUBLESHOOTING A FAILING SERVICE

When `civicmesh-mesh.service` (or `civicmesh-web.service`) won't come
up cleanly — fails on boot, flaps after promote, or starts but the
bot reports it can't reach the radio — work this ladder, cheapest
first. Most first-five-minutes failures are caught by rungs **a–c**.
For radio-side failure modes that surface only once the bot is
running, hand off to `docs/radio-debugging/failure-modes.md` after
rung **f**.

### Ladder

**a. Is it flapping or just down?**

```bash
sudo systemctl status civicmesh-mesh
```

`active (running)` is fine. `activating (auto-restart)` with a
climbing restart counter means crash-on-start — read the journal at
rung **b**. `failed` after `StartLimitBurst=5` retries within 60s
means systemd has given up; same next step. (Restart policy is
`Restart=on-failure, RestartSec=5, StartLimitBurst=5,
StartLimitIntervalSec=60` — see `apply/renderers.py`'s
`render_systemd_unit_*` functions.)

**b. What's in the journal?**

```bash
sudo journalctl -u civicmesh-mesh -e --no-pager | tail -50
```

A Python traceback means the app started but crashed on import or
init — the last frame is usually the most informative. Common ones:
`ModuleNotFoundError` (a top-level `.py` not in
`pyproject.toml`'s `py-modules` — guarded by
`tests/test_pyproject.py` post-1dd35fe), `PermissionError` reading
`/usr/local/civicmesh/etc/config.toml` (often: `configure` was run
as root in prod, leaving the file `root:root` instead of
`civicmesh:civicmesh`), or a `tomllib` parse error from a manual
edit. A systemd-side error (no Python frames; messages like
`Failed at step EXEC` or `status=200/CHDIR`) means a unit-file
problem — re-run `sudo civicmesh apply` to re-render.

**c. What do the bot's own logs say?**

If the unit got past Python startup but the bot itself reports
problems (radio unreachable, recovery firing, liveness timeouts),
the bot writes its own diagnostics to a rotating file in the
directory configured as `[logging].log_dir` in
`/usr/local/civicmesh/etc/config.toml`. The prod convention places
these under `/usr/local/civicmesh/var/logs/`:

```bash
sudo tail -100 /usr/local/civicmesh/var/logs/mesh_bot.log
```

Look for `recovery:` events, `RecoveryController` state changes,
and repeated `send_chan_msg error` lines.

**d. Who owns the serial port?**

```bash
sudo lsof /dev/ttyUSB0
```

A single PID owned by `civicmesh-mesh` is healthy port ownership —
the problem is elsewhere. **Two PIDs is the C3 conflict** (a dev
`mesh_bot` left running on the same host). See
`docs/radio-debugging/failure-modes.md` §C3. The fix is to stop the
dev process; `docs/civicmesh-tool.md` `## RUNNING DEV ALONGSIDE
PROD` documents the discipline that prevents it.

**e. Is the port even there?**

```bash
ls -l /dev/ttyUSB*
dmesg | tail -20
```

No `ttyUSB*` device at all is USB / cable / firmware-side — see
`docs/radio-debugging/boot-and-reset.md` for hardware-reset
behavior and `failure-modes.md` §D1 (CP2102 bridge hung) and §D2
(stale `ttyUSB` descriptor). Reseating the USB cable is the
cheapest first try.

**f. Radio characterization (last rung).**

Once **a–e** have ruled out non-radio causes, run the
characterization harness; its JSONL output classifies the failure
mode and is the handoff point to `failure-modes.md`. **Stop
`civicmesh-mesh` first — the harness needs exclusive serial
access:**

```bash
sudo systemctl stop civicmesh-mesh
cd /usr/local/civicmesh/app
sudo -u civicmesh python -m diagnostics.radio.recovery_characterization \
    --config /usr/local/civicmesh/etc/config.toml \
    --mode sanity \
    --out /tmp/recovery_$(date +%Y%m%d_%H%M%S).jsonl
```

### Symptom → rung

| Symptom in `journalctl` or `systemctl status` | Most likely cause | Rung |
|---|---|---|
| `ModuleNotFoundError: No module named 'X'` | Top-level `.py` not in `pyproject.toml` `py-modules`; add it (`tests/test_pyproject.py` should have caught this in CI) | **b** |
| `PermissionError` reading `/usr/local/civicmesh/etc/config.toml` | `configure` was run as root in prod; fix with `sudo chown civicmesh:civicmesh` | **b** |
| `tomllib.TOMLDecodeError` | manual edit broke the config; restore from `config.toml` backup or re-run `civicmesh configure` | **b** |
| Restart counter climbing fast (>5 in a minute) | crash on startup; read traceback | **a → b** |
| Service `active`, web UI says radio unhealthy | bot started but radio is unreachable or hung | **c → d** |
| `[Errno 2] No such file or directory: '/dev/ttyUSB0'` | USB cable, firmware crash, or CP2102 hang | **b → e** |
| Two PIDs in `lsof /dev/ttyUSB0` | dev `mesh_bot` still running on this host (C3) | **d** |
| `apply` exited with a non-zero code on deploy | match the code against the *Exit codes* table in the `apply` section above; rendered files may not be on disk | (out of band — re-run `apply`) |

### Cross-references

- Radio-side failure modes once the bot is running:
  `docs/radio-debugging/failure-modes.md` (§A ESP32, §B SX1262,
  §C serial framing, §D USB/CP2102, §E storage/prefs).
- Hardware reset behavior, boot timing: `docs/radio-debugging/boot-and-reset.md`.
- `apply` exit codes: see *Exit codes* table in the `apply` section
  above.
- Dev/prod serial-port conflict (rung **d**): `## RUNNING DEV
  ALONGSIDE PROD` above and `failure-modes.md` §C3.

---

## REPOSITORY CLEANUP (one-time, before this work begins)

The current repo has `config.toml` checked in with a real serial port
path and other machine-specific values. Before any of the changes in
this doc are implemented:

```bash
cd ~/code/CivicMesh

# Move current config.toml aside as the example:
git mv config.toml config.toml.example

# Clean up the example: remove machine-specific paths, add inline
# comments explaining each Tier 1 / Tier 2 field. (Manual edit.)

# Update .gitignore:
cat >> .gitignore <<EOF
config.toml
civic_mesh.db
logs/
.venv/
__pycache__/
*.pyc
EOF

git add .gitignore
git commit -m "Stop tracking config.toml; move to config.toml.example

config.toml is now per-machine and generated by 'civicmesh configure'.
config.toml.example is the documented reference."
```

After this, every developer's first action in a fresh checkout is:

```bash
uv sync
uv run civicmesh configure
uv run civicmesh-web --config config.toml      # smoke test
```

---

## KNOWN ISSUES & DESIGN NOTES

### `apply` does not clean up orphan files when `network.iface` changes

The networkd and NetworkManager filenames embed `<iface>`. Documented
above. Punted until external USB wifi is a real requirement (single
built-in wlan0 is the only supported config for now).

### `civicmesh stats` is much thinner than `/api/stats`

The web UI exposes ~30 fields backed by `compute_stats(...)` in
`database.py`. CLI exposes 4. Closing the gap is a small follow-up;
not part of this round.

### `[debug].allow_eth0` only affects the running app

Not enforced at the firewall layer. The nftables config allows eth0 at
all times (regardless of this flag) because the Pi Zero 2W has no
ethernet and the dev Pi 4 needs eth0 for SSH. The flag only governs
whether the web app trusts eth0-originating sessions enough to bypass
MAC validation. This is *intentional* and means flipping the flag is
safe across `apply` runs.

### Promote does not deploy config schema changes

If new code expects a config field that doesn't exist in the existing
`/usr/local/civicmesh/etc/config.toml`, promote will succeed but the
services will fail the moment they're restarted on the new code.
Operator must run `sudo -u civicmesh civicmesh configure` (or
hand-edit) and possibly `sudo civicmesh apply` *before* restarting
the services after a schema-changing promote. (This is one of the
reasons promote no longer restarts services by default — see the
`### promote` section above. The `--restart` opt-in is appropriate
for pure-code changes where the schema is known compatible; for
schema-touching PRs, the operator should configure and apply between
the promote and the restart.) We could detect schema mismatch in
promote by running `civicmesh config validate` against prod's config
with the new code's validators after the rsync — worth considering
as a follow-up, not a blocker.

---

## KNOWN GAPS (not addressed this round)                                 [GAP]

UI features without CLI counterparts. Listed for future work.

| Gap | Where | Notes |
|---|---|---|
| `messages delete <id>` | moderation | UI lets users post; admin can't remove abusive content. |
| `messages show <id>` | observability | Single-message detail with vote counts and session metadata. |
| `votes list <message_id>` | observability | Who voted on what. |
| `feedback list / show / clear` | feedback file management | `feedback.jsonl` has no CLI. |
| `pinned list` | observability | Show currently pinned messages; UI doesn't render them either. |
| Rich `stats` | observability | Match `/api/stats` field set. |
| `sessions ban <id>` / unban | moderation | No way to block a session id. |
| `mesh status` | observability | Radio state, recovery state, last-seen — currently only `/api/status`. |

Pattern: web UI has been growing; CLI has not kept up. None block
shipping `configure`/`apply`/`promote`. Worth a follow-up doc once
setup lands.

---

## SEE ALSO

- `config.toml.example` (in repo): annotated reference with all
  fields.
- `docs/hub-reference-library.md`: design doc for the hub-docs
  feature. Source of truth for the `index.json` schema, install
  procedure, validation rules, pruning semantics, and rollback —
  the *why* behind `install-hub-docs` / `rollback-hub-docs`.
- `meshcore_py-README.md`: protocol reference for the radio side.
- `failure-modes.md`, `boot-and-reset.md`, `heltec-recovery.md`:
  runtime failure handling, all of which `civicmesh apply` should
  never trigger.
