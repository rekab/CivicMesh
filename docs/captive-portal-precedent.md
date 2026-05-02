# Captive Portal Precedent

**Status:** Reference. Compiled 2026-05-01 from two independent deep-research
syntheses plus existing project notes. Drop into `docs/` and update as the
field evidence at Toorcamp comes in.

**Scope.** What captive portal detection actually is, what we tried, what
broke, and why the current design choices look the way they do. Companion to
`docs/ios-captive-portal-notes.md` and `docs/invariants.md` — this doc gives
the full picture; those give the operational rules.

---

## 1. Captive portal detection, in brief

When a client joins a Wi-Fi network, every modern OS quietly hits one or more
**probe URLs** to decide what kind of network it joined. The expected response
is a vendor-specific "success" signal. Anything else — wrong status, wrong
body, redirect, timeout — is interpreted as captive.

| Vendor      | Probe URL                                  | "Success" signal                |
|-------------|---------------------------------------------|---------------------------------|
| Android     | `clients3.google.com/generate_204`         | `204 No Content`, empty body    |
| Android     | `connectivitycheck.gstatic.com/generate_204` | same                          |
| iOS / macOS | `captive.apple.com/hotspot-detect.html`    | `200 OK`, body contains `Success` |
| iOS / macOS | `www.apple.com/library/test/success.html`  | same                            |
| Windows     | `www.msftconnecttest.com/connecttest.txt`  | `200 OK`, body `Microsoft Connect Test` |
| Windows     | `www.msftncsi.com/ncsi.txt`                | `200 OK`, body `Microsoft NCSI` |

A **walled-garden** captive portal returns 302s here, drives the user through
sign-in, then flips to returning success. Android tracks this transition
explicitly: networks have `NET_CAPABILITY_INTERNET` from the moment they
associate, but only gain `NET_CAPABILITY_VALIDATED` after a probe succeeds.
Captive networks have `INTERNET + CAPTIVE_PORTAL` and do not get
`VALIDATED`. iOS does not expose its state machine publicly but observably
behaves the same way.

This model assumes captive is a transient state on the way to "real
internet." For an offline-only network like CivicMesh, that assumption is
wrong, and pretending otherwise is the source of the failure modes below.

## 2. The two-state trap

Initial implementation (removed in CIV-82; this section is kept for context):
probe URLs returned 302 → trampoline for new clients, then flipped to
vendor-specific success bodies after the user tapped Continue at
`/portal-accept`. Mental model: "fake the standard sign-in → internet
handshake on a network that has no internet."

Why this is wrong, in one sentence: **after acceptance, the OS thinks our
SSID is real internet, then immediately tests that claim against the actual
internet and finds we cannot back it up.**

The well-documented version of this story is on Android. AOSP
`NetworkMonitor` is a state machine. A successful probe transitions a network
to `ValidatedState`. From there:

- Sustained DNS failures, consecutive timeouts, or failed re-probes trigger
  re-evaluation.
- Failed validation backs off exponentially: 1s, 2s, 4s, … capped at 10
  minutes.
- Captive-state networks are rechecked every 10 minutes.
- A network demoted back to invalid/unwanted loses default-network
  preference; new connections route over whatever else is available
  (typically cellular).

So the lifecycle is: probe → 204 → "validated" → app makes real DNS lookup
for `clients3.google.com` → DNS hijack returns `10.0.0.1` → TCP connection
goes nowhere or gets blackholed → repeat → demotion → cellular fallback. On
iOS the analogous mechanism is Wi-Fi Assist plus the CNA's auto-revalidation
on resume; the public API surface is thinner but the symptom is identical.

The fix is to **stay captive forever.** The OS-facing probe URLs return 302
to trampoline every time, for every IP, regardless of any in-app state. The
trampoline / "Continue" UI still exists, but it changes the *application's*
state, not the network state the OS sees.

This is not a hack. It is the truthful description of the network: **the
network is permanently a walled garden with nothing past the wall.** openNDS
documents this exact mode for offline preauth clients. Microsoft's NCSI
guidance says explicitly "don't mix behaviors — keep redirecting consistently
until authenticated." ChromeOS treats anything other than a 204 as portal
state. The legacy `LibraryBox` / `PirateBox` projects went the other way (see
§9) and paid for it.

## 3. `.local` and mDNS — the unicast/multicast split

We initially used `civicmesh.local` as the canonical portal host. Android
worked. iOS hung. Diagnostic timeline in `ipad-wifi-debug-checkpoint.md`,
design write-up in `docs/ios-captive-portal-notes.md`.

Root cause: RFC 6762 reserves `.local` for multicast DNS. Apple platforms
implement that reservation strictly — `.local` queries go out as multicast
to `224.0.0.251:5353` and *never* hit unicast DNS. Our `dnsmasq`
wildcard hijack (`address=/#/${AP_IP}`) only sees unicast queries, so the
hijack never fires and Safari hangs waiting for an mDNS responder we don't
run.

Android sends unicast queries for `.local` names by default, which is why
this bug was invisible in dev. **"Works on Android" is not coverage for any
DNS-related captive-portal change. Test iOS explicitly.**

Three options were considered:

1. Use the AP IP directly (`http://10.0.0.1/`). Simplest, works everywhere,
   no new services. Aesthetic cost: ugly URL bar.
2. Use a non-`.local` fake TLD (`.internal`, `.lan`, `.hub`). Resolves
   through the existing wildcard hijack on all platforms. Cost: squatting
   on TLDs we don't own (`.hub` is a real public TLD), and a maintainer
   later wondering "what is this." `.internal` is the most defensible —
   ICANN reserved it for private use in 2024.
3. Run avahi as an mDNS responder. Adds a daemon, attack surface, and a
   subtle interaction with iOS captive-portal probes during the
   pre-connection mDNS phase.

Decision: option 1, raw IP, sourced from `config.network.ip`. Codified as an
invariant: **portal hostnames must not use `.local`.** This applies to probe
redirects, Host-header gating, `/portal-accept` redirect targets, the
trampoline page, and any RFC 8908-style API response. The post-CIV-60
config schema makes the AP IP the single source of truth.

The recommendation in one of the deep-research reports to add an mDNS
responder for `hub.local` was specifically rejected for these reasons.

## 4. HTTPS: why we don't have it, and what follows

The portal is HTTP-only. There is no path to HTTPS on this device, and the
absence of HTTPS shapes a surprising number of downstream choices.

Why no HTTPS:

- The AP is isolated. There is no DNS path that lets us prove ownership to a
  public CA. Let's Encrypt etc. are out.
- The portal IP is RFC1918. Public CAs do not issue certs for private IPs.
- We could ship a self-signed cert and a CA-install workflow, but a walk-up
  emergency hub is the worst possible UX context for "click through five
  scary warnings to install our root certificate."
- Nothing the portal handles is sensitive. Sessions are display-name +
  rough location; no passwords, no PII, no financial data. The threat model
  for an offline community message board on an emergency drill SSID is
  spam, not eavesdropping.

What follows:

- **No RFC 8908 / 8910.** RFC 8908 requires the captive-portal API URL be
  HTTPS. Lenient clients that accept an HTTP URL would parse our response
  and see `captive=false` (or whatever we sent) and suppress the login flow
  entirely; strict clients ignore the option. Either way it makes things
  worse, not better. `dhcp-option=114` is intentionally not advertised.
  Documented in the inline comment in
  `apply/renderers.py:render_dnsmasq_conf`.
- **Port 443 on the AP iface should be `reject`ed fast, not just dropped.**
  With wildcard DNS hijacking, every background HTTPS / QUIC connection on
  every connected phone resolves to the AP IP. We never want those to reach
  a TLS terminator (we have no cert), and we want client stacks to give up
  immediately rather than burning battery on retries. The choice between
  DROP and REJECT here is *not* "TLS error vs no TLS error" — both prevent
  the connection from completing before TLS, so neither produces certificate
  warnings. The real tradeoff is fast-fail (RST / ICMP unreachable) versus
  slow timeout (silent DROP). Fast-fail wins for walk-up phones: TCP gets
  an immediate RST and stops retrying; QUIC needs the negative ICMP signal
  to abort cleanly instead of retransmitting until a multi-second timeout.
  As of CIV-82 the firewall has explicit rules: `tcp dport 443 reject with
  tcp reset` and `udp dport 443 reject` on the AP iface. The `with tcp
  reset` clause is required — nftables' default TCP rejection sends ICMP
  unreachable, which TCP stacks treat very differently from a RST.
- **Probe URLs are HTTP.** All five vendors run their captive-detection
  probes over HTTP precisely because TLS would defeat the purpose
  (interception). This is the one piece of the system where the
  HTTP-only constraint is *helpful*.

## 5. DNS hijack design

`dnsmasq` config:

```
address=/#/${AP_IP}     # answer every name with the AP IP
no-resolv               # never forward upstream
no-poll                 # ignore /etc/resolv.conf changes
cache-size=0            # we return the same answer for everything
```

The `/#/` wildcard syntax means "match any domain." We answer every query
with `10.0.0.1`. There is no upstream DNS configured; that is the correct
state for an offline portal. (Claude has burned time in past sessions
treating "no upstream DNS" as a bug to fix. It isn't.)

Caveat already covered in §3: this only catches unicast queries. iOS sends
mDNS for `.local`, which never reaches dnsmasq.

One of the deep-research reports recommends narrowing the hijack to known
captive-probe domains only and returning NXDOMAIN for everything else, with
the rationale that this lets iOS's "Use Without Internet" mode cleanly
split-tunnel to cellular for non-portal traffic. This is a reasonable design
for a deployment with reliable LTE alongside the local SSID — it reduces the
weirdness when a user keeps the SSID joined alongside cellular. **It is not
a fit for the Toorcamp deployment**, where there is no cellular at most
gathering spots; if NXDOMAIN sends traffic looking for cellular and there is
no cellular, the user is just broken. The wildcard hijack is the right call
when "no other network" is the default condition. Revisit for the Seattle
hub deployment, where users typically *do* have LTE alongside.

## 6. Platform-specific quirks

### iOS

- Captive Network Assistant (CNA) is a stripped-down WebKit sheet, not Safari.
  Local storage works; some advanced features don't. LibraryBox abandoned
  it for being too limited; we accept it because the portal UI is simple.
- "Use Without Internet" is a real first-class state. The device stays
  associated, Auto-Login is turned off, and the device "can still use the
  network in other ways." Re-entry is via Settings → Wi-Fi → More Info →
  Join Network — it is *not* a "tap the network notification again" flow.
  Trampoline copy should make this state legible to users.
- `.local` is mDNS-only. See §3.
- iOS Wi-Fi Direct chatter (AirDrop, AirPlay, Continuity) tickles
  `brcmfmac` P2P paths. On Pi 4 with kernel `6.12.47+rpt-rpi-v8` this
  triggered `Oops: NULL pointer dereference` in
  `brcmf_p2p_send_action_frame`, leaving the driver wedged and unrelated
  shell commands going into D-state. Mitigated by `apt full-upgrade` to
  `6.12.75+`. Not yet verified on Pi Zero 2W (BCM43436 vs Pi 4's BCM43455);
  open question in `docs/open_questions.md`.

### Android

- AOSP exposes the state machine, retry cadence, and notification flow in
  source. This is the platform we have the most documented confidence
  about. See §2.
- Per-SSID randomized MAC rotates on reconnection. Sessions cannot
  fingerprint by MAC alone. We log mismatches but accept-and-update.
- Three captive-portal user actions: dismiss + re-evaluate, ignore + prefer
  other networks, "use as is" (warning: app connectivity may be disrupted).
- Did not find documentation guaranteeing the captive-portal sign-in
  notification survives a device reboot. Treat re-entry as best-effort.

### Windows / ChromeOS

- Less critical for our walk-up audience, but probe handlers need to cover
  `connecttest.txt` and `ncsi.txt`.
- Microsoft's NCSI guidance: keep behavior consistent. Don't redirect some
  things and drop others.
- Once 443 is DROPed (see §4), Windows browsers handle the "no HTTPS"
  reality gracefully — they time out the HTTPS attempt and the user
  navigates to HTTP manually if needed. ChromeOS is similar.

## 7. Patterns from related projects

**openNDS / nodogsplash.** The modern lineage. Documents permanent-captive
as a real operational mode and explicitly says preauth clients do not need
internet access for the portal to work. Closest existing model to what
CivicMesh actually is.

**PirateBox / LibraryBox.** Earlier offline file-sharing hubs. Their
maintainers tried the "fake a 204 success after acceptance" pattern (PirateBox
changelog confirms this) and abandoned it as mobile OSes added background
validation. LibraryBox went the other direction on iOS and bypassed the CNA
entirely, sending users to a real browser with a literal local URL — they
found the captive sheet too limited for their UI. Useful contingency to keep
in mind: if the CNA mini-browser ever turns out to be too constrained for the
CivicMesh portal UI, the fallback is "stop relying on the CNA, train users
to open a real browser at `http://10.0.0.1/`."

**NYC Mesh.** Not directly comparable (they have an uplink), but their
captive-portal failure modes when the uplink dies are exactly what an
intentionally-offline node looks like in steady state — useful sanity check
that "permanent captive" is at least operationally familiar.

## 8. Open items

- Whether the `brcmfmac` P2P crash reproduces on Pi Zero 2W (BCM43436)
  the way it did on Pi 4 (BCM43455). Bench test before any field deploy.
- Whether to narrow the DNS hijack from wildcard to probe-domains-only for
  the Seattle hub deployments, where users typically have LTE alongside
  the local SSID. Wildcard stays for Toorcamp.
- Field-validate that always-captive (post-CIV-82) cleanly avoids the
  cellular-fallback failure mode under realistic conditions. The mechanism
  is well-documented but the deployment context is novel.
- Trampoline copy. Right now it's pleasantries; should explicitly state
  "no internet by design — local message board only" so the captive state
  is the *expected* state, not a surprise.
