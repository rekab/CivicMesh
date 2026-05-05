# CivicMesh Open Questions

## ~~MeshCore send semantics~~
- ~~Why it matters: defines what "sent" means and how to report status to users.~~
- ~~Assumption for v0: meshcore_py returns only local send success/failure.~~
- ~~Validate: simulate send with radio attached; inspect return codes and timings.~~

## ~~Delivery signals availability (ACKs, repeats)~~
- ~~Why it matters: impacts status UI and retry/backoff decisions.~~
- ~~Assumption for v0: not reliably available.~~
- ~~Validate: field test with two radios; record any metadata for repeats/acks.~~

## ~~Channel subscription behavior~~
- ~~Why it matters: impacts inbound filtering and storage volume.~~
- ~~Assumption for v0: subscribe only to configured list; ignore others.~~
- ~~Validate: configure a known channel list; verify inbound filtering in logs.~~

## ~~Serial reliability under power loss~~
- ~~Why it matters: recovery and reconnection strategy.~~
- ~~Assumption for v0: mesh bot can reconnect on restart.~~
- ~~Validate: power-cycle tests; measure reconnection time and failure rate.~~

## ~~Throughput vs. rate limits~~
- ~~Why it matters: caps must protect the mesh without blocking normal use.~~
- ~~Assumption for v0: conservative limits (per-session per minute or hour).~~
- ~~Validate: simulate bursts; observe queue growth and UI responsiveness.~~

## SQLite write endurance
- Why it matters: SD card wear and power use.
- Assumption for v0: batching and pruning are sufficient.
- Validate: long-duration write tests; monitor I/O and database size growth.

## ~~Captive portal behavior across OSes~~
- ~~Why it matters: portal must open reliably on iOS/Android.~~
- ~~Assumption for v0: HTTP + common captive portal triggers are enough.~~
- ~~Validate: test on iOS and Android captive portal detection.~~

## Does the brcmfmac P2P crash reproduce on Pi Zero 2W
Does the brcmfmac P2P crash reproduce on Pi Zero 2W (BCM43436) as it does on 
Pi 4 (BCM43455)? Needs explicit testing before deployment.

## Redundant host-header check in do_GET
- Where: `web_server.py` lines ~442-471
- When `path == "/"`, the host-header check runs twice: once in the
  `if path == "/"` block (line 443) and again in the
  `if path == "/" or path.startswith("/static/")...` block (line 461).
  The second check is unreachable for `/` because the first block already
  returns on mismatch or falls through on match then hits the same condition.
- Not a bug, just wasted work on every `/` request.
- Fix: refactor into `if/elif/else` so `/` is handled once. Deferred from
  the portal_host branch to keep that PR minimal.

## ~~Consider using a friendly hostname instead of raw IP for portal_host~~
~~Current: `portal_host = "10.0.0.1"`. Works but ugly in the URL bar.~~

~~Option: `portal_host = "civicmesh.internal"`. `.internal` was reserved
by ICANN in 2024 for private networks. dnsmasq wildcard hijack already
answers any name, so no server-side changes required. iOS does unicast
DNS for non-`.local` names, so the resolution path works.~~

~~Blocked by: nothing technical. Just want to verify the IP-based portal
works end-to-end first before changing the variable. Revisit after
merging fix-ios-wifi.~~
