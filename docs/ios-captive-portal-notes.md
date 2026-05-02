# iOS Captive Portal Notes

This document covers iOS-specific issues encountered when getting an
iPad (or iPhone) onto the CivicMesh AP and through the captive portal.
Two separate problems manifested during debugging on 2026-04-12; both
are documented here because a future maintainer searching for "iPad
broken" will want all the iOS-specific context in one place.

1. [`.local` hostnames and iOS mDNS](#local-hostnames-and-ios-mdns) — a
   design decision about portal hostname choice.
2. [`brcmfmac` kernel crash triggered by iOS](#brcmfmac-kernel-crash-triggered-by-ios) —
   a driver bug that blocks iPads from staying connected on affected
   kernel versions.

---

## `.local` Hostnames and iOS mDNS

**Status:** Decided, 2026-04-12
**Related:** `docs/captive_portal_setup.md`, `docs/invariants.md`

### Problem

iOS devices connecting to the CivicMesh AP associate and obtain a DHCP
lease cleanly, but the portal page fails to load. Safari rewrites
`http://10.0.0.1/` to `civicmesh.local` in the URL bar and hangs. No
response ever arrives. Android works correctly under identical conditions.

This was first observed during iPad debugging on 2026-04-12 and blocked
captive portal use on all iOS devices.

### Root Cause

Apple devices treat `.local` hostnames as **mDNS (multicast DNS / Bonjour)
territory**, not as unicast DNS names. This behavior is specified in
RFC 6762 (Multicast DNS), which reserves `.local` for link-local name
resolution, and is implemented by Apple platforms in the strict sense:
iOS does **not** send unicast DNS queries for `*.local` names. It sends
mDNS multicast queries to `224.0.0.251:5353` instead.

CivicMesh's DNS hijacking strategy — `address=/#/10.0.0.1` in
`/etc/dnsmasq.d/civicmesh.conf` — relies on clients sending unicast DNS
queries to the Pi, which answers every query with the AP's IP. For iOS
devices resolving `civicmesh.local`, that query **never reaches dnsmasq**.
iOS multicasts an mDNS query, nothing on the network answers (we don't
run an mDNS responder), and Safari hangs waiting for resolution.

Android handles `.local` differently: it sends unicast DNS queries for
`.local` names by default, which is why the hijack worked for Android and
masked this bug during initial development.

### Why It Matters for Captive Portal Flow

`web_server.py` previously used `civicmesh.local` as the canonical host
for the portal. Incoming HTTP requests with a different `Host:` header
(e.g., `Host: 10.0.0.1`, or any hostname from a captive portal probe
like `captive.apple.com`) were 302-redirected to `http://civicmesh.local/`
so that the rest of the app could assume a consistent hostname for URL
generation, cookies, and relative links.

On iOS, this redirect is a trap: the browser follows the 302, tries to
resolve `civicmesh.local`, can't, and hangs. The user sees a frozen
Safari tab and eventually gives up.

### Options Considered

#### 1. Use the AP IP directly (`10.0.0.1`)

Replace every reference to `civicmesh.local` with the AP IP. The portal
URL becomes `http://10.0.0.1/`, and the Host-header gating compares
against `10.0.0.1`.

**Pros:**
- Simplest possible fix. No DNS required for the redirect target.
- Works identically on every client platform.
- No new services, no new attack surface, no extra power draw.
- The AP IP is already a known constant — `10.0.0.1`, hardcoded in
  the rendering pipeline (`apply/renderers.py`).

**Cons:**
- The URL bar shows `10.0.0.1` instead of a friendly name. For a captive
  portal where users are clicking through a splash page rather than
  typing URLs, this is a minor aesthetic cost.
- Hardcodes an IP in application code. Mitigated by passing the IP
  through config rather than literal strings.

#### 2. Use a non-`.local` fake TLD

Pick a name like `civicmesh.internal`, `civicmesh.lan`, `civicmesh.hub`,
or `portal.civicmesh`. The existing dnsmasq wildcard hijack
(`address=/#/10.0.0.1`) already answers every query, so any non-`.local`
name will resolve correctly on all platforms.

**Pros:**
- Preserves a friendly name in the URL bar.
- No code changes beyond the hostname string.

**Cons:**
- Requires picking a TLD we don't own. `.internal` was reserved by
  ICANN in 2024 specifically for private-use networks and is the most
  technically correct choice. `.lan` is conventional but unofficial.
  `.hub` is a real public TLD (registry: `dotHub Limited`), so using it
  is squatting on a name that has meaning elsewhere on the internet.
- Users may wonder why a familiar-looking address doesn't work elsewhere
  and be briefly confused.
- Adds a layer of "why does this exist" that a maintainer has to
  re-derive if the choice isn't documented.

#### 3. Run an mDNS responder (avahi)

Install and run `avahi-daemon` on the Pi, configured to answer mDNS
queries for `civicmesh.local` with the AP IP. This makes `.local` work
on iOS by speaking the protocol iOS actually uses for those names.

**Pros:**
- Preserves the original `.local` naming scheme.

**Cons:**
- Adds a new daemon to an already-tight resource budget on the Pi Zero 2W.
- More configuration surface, more attack surface, more things that can
  fail silently.
- mDNS and captive portal detection interact in subtle ways on iOS. Some
  iOS versions have behaved unpredictably when captive portal probes
  occur during the mDNS pre-connection phase. Debugging those
  interactions is not a good use of project time.
- mDNS traffic consumes airtime on a constrained WiFi AP.

### Decision

**Use the AP IP directly (Option 1).**

- Portal hostname is `10.0.0.1` (the AP IP, hardcoded in the
  rendering pipeline).
- All references to `civicmesh.local` in application code are replaced
  with a configurable value sourced from `config.toml`, so that the IP
  is defined in exactly one place and the deployment script and
  application agree.
- User-facing copy continues to use the name "CivicMesh" for branding;
  only the URL uses the IP.

Rationale:

1. Reliability across devices matters more than URL aesthetics for a
   captive portal that users reach by tapping a notification, not by
   typing.
2. The project's power and complexity budget (Pi Zero 2W, emergency
   deployment context, no internet) argues strongly against adding
   services like avahi that introduce failure modes we can't easily
   test in advance.
3. The friendly-name benefit of a fake TLD is small and comes with
   real costs in future maintainer confusion and TLD squatting.
4. The AP IP is already the source of truth in the network setup; the
   application should follow, not introduce a parallel naming scheme.

### Invariant

**Portal hostnames must not use `.local`.** iOS treats `.local` as
mDNS-only and bypasses unicast DNS entirely, making DNS hijacking
ineffective for Apple devices. Any hostname used in portal redirects,
Host-header gating, or user-facing URLs must be either a raw IP or a
non-`.local` name answerable by unicast DNS.

Added to `docs/invariants.md`.

### Related Android Behavior

Android's handling of `.local` names has varied over versions, but
Android in its current form sends unicast DNS queries for `.local`
hostnames unless mDNS is explicitly configured and answers come back.
This is why Android worked during initial CivicMesh development despite
the same underlying hostname choice. Do not take "works on Android" as
validation of a DNS-hijack-only approach; iOS is the stricter case and
must be tested explicitly.

### References

- RFC 6762 — Multicast DNS (defines `.local` semantics)
- RFC 6761 — Special-Use Domain Names
- ICANN reservation of `.internal` for private use (2024)
- Apple Bonjour / NSNetService documentation on `.local` handling

---

## `brcmfmac` Kernel Crash Triggered by iOS

**Status:** Mitigated by kernel upgrade, 2026-04-12
**Related:** `docs/captive_portal_setup.md`, `docs/invariants.md`

### Problem

On a Raspberry Pi 4 running Raspberry Pi OS with kernel
`6.12.47+rpt-rpi-v8`, the kernel crashed (`Oops: NULL pointer
dereference`) inside the Broadcom WiFi driver whenever an iPad was
connected to (or in close proximity to) the CivicMesh AP. Two reproductions
were observed during a single debug session:

1. Once immediately after `systemctl restart hostapd`.
2. Once during steady-state operation, ~20 minutes into a session with
   an iPad associated.

Each crash left the `brcmfmac` driver in an undefined state. Processes
that touched any code path tangled with the wedged driver entered
uninterruptible sleep (`D` state) and could not be killed — including
apparently unrelated commands like `grep` against files in `/etc`. A
reboot was required to recover.

### Symptoms

- Kernel oops in `dmesg` with the trace pointing at
  `brcmf_p2p_send_action_frame`:
  ```
  Internal error: Oops: 0000000096000005 [#1] PREEMPT SMP
  pc : brcmf_p2p_send_action_frame+0x234/0xc98 [brcmfmac]
  lr : brcmf_p2p_send_action_frame+0x200/0xc98 [brcmfmac]
  Call trace:
    brcmf_p2p_send_action_frame+0x234/0xc98 [brcmfmac]
    brcmf_cfg80211_mgmt_tx+0x300/0x5c0 [brcmfmac]
    cfg80211_mlme_mgmt_tx+0x170/0x420 [cfg80211]
    nl80211_tx_mgmt+0x24c/0x3b8 [cfg80211]
  ```
- Kernel tainted (`Tainted: G WC`) after the first oops.
- iPad shows "briefly connected, then bailed back to another network"
  behavior, because the WiFi stack is partially dead.
- Unrelated shell commands start hanging in `D` state shortly after the
  oops, because D-state contagion spreads through any code path that
  touches the wedged driver.

### Root Cause

A NULL pointer dereference in the `brcmfmac` driver's P2P (Wi-Fi Direct)
action frame handling. The P2P code path is exercised when an iOS device
is nearby and broadcasting/responding to P2P action frames for AirDrop,
AirPlay, Handoff, and other Continuity features. Apple platforms are
aggressive about P2P chatter on associated WiFi networks; the driver's
P2P path has NULL-deref issues in certain kernel/firmware combinations
and crashes when it tries to dispatch these frames.

Android devices do far less Wi-Fi Direct activity in the same scenario,
which is why the bug was not caught earlier and why Android remained
stable on the same hardware and kernel.

**This is a known family of bugs in `brcmfmac`**, not a CivicMesh issue
and not something the project can fix. The driver is the open-source
Linux driver for Broadcom WiFi chips; Broadcom does not actively maintain
it, and the firmware is a closed blob. Bugs in the P2P code path have
surfaced periodically over the years with various specific symptoms.

### Resolution (for the affected session)

Running `sudo apt update && sudo apt full-upgrade` pulled a newer kernel
(`6.12.75+rpt-rpi-v8`) along with updated `raspi-firmware` and related
packages. After a reboot, the crash stopped reproducing even under the
same workload (iPad connected, active, left running for a soak test).

Note that `apt upgrade` (without `full-upgrade`) is **not sufficient**:
on Raspberry Pi OS, kernel packages are routinely held back by plain
`upgrade` because they have changed dependencies. `full-upgrade` is
required to actually pull the new kernel.

### What We Don't Know

This section exists because we should not overstate our knowledge.

- **We don't know which specific change fixed it.** `apt full-upgrade`
  updated the kernel, possibly the Broadcom firmware blob, and several
  other packages. We did not attempt to isolate the fix to a specific
  component.
- **We don't know a minimum working kernel version.** We know
  `6.12.47+rpt-rpi-v8` is broken and `6.12.75+rpt-rpi-v8` is not. The
  boundary could be anywhere in between, and a future regression is
  possible if upstream reintroduces a similar bug.
- **We don't know if the Pi Zero 2W is affected.** The Zero 2W uses the
  BCM43436 chip, while the Pi 4 we tested on uses BCM43455. The drivers
  share code but also have chip-specific paths. The symptom may or may
  not reproduce on the deployment hardware. This needs explicit testing
  on Zero 2W before field deployment.
- **We don't know if the bug is truly fixed or just masked.** The code
  path that crashed (`brcmf_p2p_send_action_frame`) still exists. A
  different iOS device, iOS version, or usage pattern might exercise it
  differently and re-expose the bug.

### Mitigation Strategy

**Required as part of provisioning** (in order of cost):

1. **Run `apt full-upgrade` during initial Pi provisioning**, before
   running `scripts/civicmesh-bootstrap.sh`. Reboot afterward.
   This is the cheapest mitigation and resolved the issue during
   debugging. Plain `apt upgrade` is insufficient because it holds back
   kernel packages.

2. **Test with an actual iPad** before declaring deployment ready.
   Android stability is not sufficient coverage. An iOS device should
   be able to connect, stay connected, and use the portal for at least
   several minutes without triggering `dmesg` entries about
   `brcmfmac` or kernel taint.

3. **Check `dmesg` after any suspected hang:**
   ```bash
   dmesg -T | grep -iE 'brcmfmac|oops|taint'
   ```
   An oops here means the driver crashed and the device needs a reboot.
   No amount of userspace debugging will recover a wedged `brcmfmac`.

**Escalation path if the kernel upgrade stops being sufficient** (in
order of disruption):

1. **External USB WiFi dongle for AP role.** A ~$10 dongle with a
   well-supported chipset (RTL8188EUS, MT7601U, RT5370, etc.) bypasses
   `brcmfmac` entirely. `hostapd` runs on the dongle's interface; the
   onboard radio stays idle. Many Pi-based AP projects ship this way
   because `brcmfmac` reliability issues are well-known. The main cost
   is an additional USB port and part to source.

2. **Pin to a known-good kernel version** if newer kernels reintroduce
   the bug. This is a last resort — pinning kernel versions on
   Raspberry Pi OS creates maintenance debt and blocks security
   updates.

Not recommended, tried and rejected:

- **`options brcmfmac p2pon=0`** in `/etc/modprobe.d/`. This parameter
  enables *legacy* P2P management functionality and is off by default;
  setting it to 0 is a no-op. The crash is in the non-legacy P2P code
  path, which is not configurable via module parameter. This was
  proposed during debugging but turned out to be the wrong knob.

### D-State Contagion: An Aside

The crash has a secondary symptom worth documenting because it confused
debugging for some time: **`grep`, `cat`, and other trivial commands
hung in `D` state after the oops, even against files that had nothing
to do with wireless.** This is not a filesystem bug; it is the expected
kernel behavior when a process enters a kernel code path that touches
the wedged driver subsystem.

If you see this pattern — fresh shells wedging on simple commands after
an oops has occurred — **do not investigate the filesystem**. Check
`dmesg` for oopses first, and if found, plan a reboot. The filesystem
is fine.

### References

- Linux kernel `brcmfmac` driver source:
  `drivers/net/wireless/broadcom/brcm80211/brcmfmac/p2p.c`
- `raspberrypi/linux` GitHub issue tracker (search for `brcmfmac p2p`)
- Raspberry Pi forums — threads on `brcmfmac` crashes with AP mode and
  iOS clients appear periodically
