# CivicMesh Open Questions

## MeshCore send semantics
- Why it matters: defines what "sent" means and how to report status to users.
- Assumption for v0: meshcore_py returns only local send success/failure.
- Validate: simulate send with radio attached; inspect return codes and timings.

## Delivery signals availability (ACKs, repeats)
- Why it matters: impacts status UI and retry/backoff decisions.
- Assumption for v0: not reliably available.
- Validate: field test with two radios; record any metadata for repeats/acks.

## Channel subscription behavior
- Why it matters: impacts inbound filtering and storage volume.
- Assumption for v0: subscribe only to configured list; ignore others.
- Validate: configure a known channel list; verify inbound filtering in logs.

## Serial reliability under power loss
- Why it matters: recovery and reconnection strategy.
- Assumption for v0: mesh bot can reconnect on restart.
- Validate: power-cycle tests; measure reconnection time and failure rate.

## Throughput vs. rate limits
- Why it matters: caps must protect the mesh without blocking normal use.
- Assumption for v0: conservative limits (per-session per minute or hour).
- Validate: simulate bursts; observe queue growth and UI responsiveness.

## SQLite write endurance
- Why it matters: SD card wear and power use.
- Assumption for v0: batching and pruning are sufficient.
- Validate: long-duration write tests; monitor I/O and database size growth.

## Captive portal behavior across OSes
- Why it matters: portal must open reliably on iOS/Android.
- Assumption for v0: HTTP + common captive portal triggers are enough.
- Validate: test on iOS and Android captive portal detection.
