# CivicMesh Power Budget

**Hardware under test:** Raspberry Pi Zero 2W + Heltec V3 (USB serial) + UGREEN PB205 (25,000 mAh / ~90 Wh)
**Measurement window:** Apr 16–19, 2026 (Week 1 characterization)
**Goal:** Establish trustworthy Wh/hour number to validate the 36-hour battery swap cadence planned for Toorcamp deployment.

---

## Headline numbers

| Scenario | Avg draw | Runtime on 90 Wh | Notes |
|---|---|---|---|
| Light mixed load (manual interaction, mostly idle) | **~2.1 W** | ~43 hrs | Representative of a quiet Hub |
| Sustained 3-client synthetic load (GET-only) | **~2.9 W** | ~31 hrs | Pessimistic upper bound — unrealistic sustained hammering |
| Expected real-world Hub load (bursty walk-ups) | ~2.2–2.5 W (est.) | ~36–41 hrs | Between the two measured points, closer to light |

**Verdict:** UGREEN PB205 + 36-hour swap cadence is viable for expected real-world Hub load. Worst-case sustained load lands at ~31 hrs, which would tighten the swap cadence but does not break the deployment model.

---

## Test 1 — Light mixed load, 28-hour run

**Setup:** Pi Zero 2W running CivicMesh stack, mixed manual testing (some browsing, mostly idle), Heltec V3 connected via USB serial doing mesh RX.

**Duration:** 28 hrs
**UGREEN:** 100% → 33%
**Energy drawn:** ~60 Wh
**Average draw:** **~2.1 W**

Implied runtime on full 90 Wh pack: ~43 hrs. This is the "typical quiet Hub" baseline.

---

## Test 2 — UGREEN pass-through + recharge validation

**Setup:** At 26% UGREEN remaining (23:20, 31h 37m uptime), plugged a Jackery Explorer 1000 (71%) into the UGREEN's input while Pi continued to draw from UGREEN's output.

**Duration:** ~7.5 hrs overnight
**Result:** UGREEN 26% → 100%, Jackery 71% → 61% (~100 Wh delivered)
**Pi:** Zero pings dropped, uptime uninterrupted

**Conclusions:**
- UGREEN PB205 pass-through works under real load — the pack accepts charge on input while delivering to the Pi on output, with no interruption.
- No thermal drama at the pack during simultaneous charge + deliver.
- Field recharge model is viable: a Jackery or similar can fully top up a UGREEN overnight while the node stays online.

---

## Test 3 — Sustained 3-client load, 12-hour overnight run

**Setup:**
- 3 synthetic HTTP clients running from a Mac on the Pi's WiFi AP
- Each client: `GET /api/messages?channel=<name>&limit=50` every 10–30 seconds
- Two channels rotated: `#civicmesh` (~14 KB responses) and `#testing` (~19 KB responses)
- **No POSTs were made** — `POST_ENABLED = False` in the loadgen config. This deliberately avoided posting junk messages to the LoRa mesh or spamming other MeshCore nodes on the channel.
- Load generator: `diagnostics/loadgen.py` (stdlib-only, checked into repo)

**Duration:** 10.1 hrs (21:40 → 07:46, wall-clock)
**UGREEN:** 98% → 65%
**Energy drawn:** ~29.7 Wh
**Average draw:** **~2.94 W**

### Pi-side telemetry (sampled every 60s on the Pi)

Monitoring command:
```bash
while true; do
  echo "$(date -Iseconds) $(uptime | awk -F'load' '{print $2}')     temp=$(vcgencmd measure_temp)     volt=$(vcgencmd measure_volts)     throttled=$(vcgencmd get_throttled)"
  sleep 60
done | tee -a overnight.log
```

**629 samples collected across the run.**

| Metric | Value |
|---|---|
| CPU temperature (min / avg / max) | 42.9°C / 44.9°C / 47.8°C |
| Throttling events | **0** (all samples `throttled=0x0`) |
| 1-min load average (modal) | < 0.10 across the vast majority of samples |
| Undervoltage events | 0 |
| Pi reboots | 0 |
| Pings dropped (from the Mac) | 0 |

### Key finding: CPU is not the bottleneck

The load average distribution during sustained 3-client serving stayed near zero. 78 samples at 0.00, 51 at 0.01, tapering down. This means the Pi Zero 2W handled the SQLite queries and JSON serialization for 3 concurrent clients with trivial CPU cost. The ~0.85 W delta above the light-load baseline is almost certainly attributable to **WiFi radio activity** (sustained TX of 14–19 KB responses every ~20 seconds across 3 clients) and **SD card I/O**, not processing.

**Implication for design:** incremental Python-side work (additional endpoints, richer queries, JSON encoding) is not where the power goes. The watts-per-client curve is dominated by WiFi airtime and SQLite I/O patterns.

---

## Test 4 — Long unattended uptime (subsumes the 36-hour checklist item)

Across the combined test window, the Pi ran **65+ hours unattended** (first with light load, then with Jackery top-up, then sustained synthetic load). No reboots, no wedges, no throttling events.

The Week 1 checklist item "36-hour unattended UGREEN runtime test" is satisfied by this continuous run.

---

## Power math reference

UGREEN PB205 nominal capacity: **90 Wh** (25,000 mAh × 3.7 V, before conversion losses).

Measured correspondence from Test 2's full recharge cycle: **~0.9 Wh per UGREEN %** (74% charge accepted ≈ 67 Wh delivered).

| Avg draw | Runtime on 90 Wh | Swap cadence |
|---|---|---|
| 1.5 W | ~60 hrs | Every 2 days, comfortable |
| 2.0 W | ~45 hrs | Every 36 hrs, target |
| **2.1 W (measured light)** | **~43 hrs** | **Every 36 hrs, on target** |
| 2.5 W | ~36 hrs | Every ~30 hrs, tight but OK |
| **2.9 W (measured sustained)** | **~31 hrs** | **Every ~28 hrs, painful** |
| 3.0 W | ~30 hrs | Every day, painful |

---

## Conclusions

1. **UGREEN PB205 + Pi Zero 2W + Heltec V3 is the right hardware stack.** No failures, no thermal issues, no undervoltage events across 65+ hrs of mixed testing.

2. **36-hour swap cadence is viable** for expected real-world Hub load. Even in the pessimistic 2.9 W case, a single pack buys ~31 hrs — a pack swap every 24 hrs during Toorcamp would provide margin.

3. **Pass-through recharge works.** Field operators can top up a node from shore power (or a larger Jackery) without interrupting service.

4. **CPU and thermals are not limiters.** Design tradeoff focus should be on WiFi client count and SQLite I/O, not Python processing cost.

5. **Solar is not required at Toorcamp** (though nice-to-have as a supplement). Two UGREEN packs per node, rotated via Jackery overnight, covers the 4-day event with margin.

---

## Artifacts

- **`diagnostics/loadgen.py`** — stdlib-only synthetic load generator used for Test 3
- **`overnight.log`** — 629 samples of Pi-side telemetry from Test 3
- **`loadgen.log`** — per-request logs from the Mac side during Test 3
- **`ping.log`** — drop-detection ping from the Mac side during Test 3

---

## Open questions / still to do in Week 1

- [ ] Enable hardware watchdog (`/dev/watchdog` + systemd)
- [ ] Add nightly cron reboot at 4am
- [ ] Mesh TX burst peak measurement (short, targeted test — needs USB power meter inline)

## Deferred to later weeks

- Idle draw with zero connected WiFi clients (would establish the true floor; not critical for deployment math)
- Power profile inside sealed IP67 enclosure (Week 2, with thermal implications)
- Real-world walk-up load profile at a Hub (Week 4 outdoor test)
