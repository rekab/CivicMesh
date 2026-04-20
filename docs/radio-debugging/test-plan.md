# Test plan

Empirical tests to disambiguate the surviving hypotheses in [`our-hang.md`](our-hang.md) and resolve the open questions raised in [`boot-and-reset.md`](boot-and-reset.md) and [`failure-modes.md`](failure-modes.md).

## Common setup

Used by most tests. Assemble once, reference.

- **Device under test (DUT):** Heltec V3 running `companion-v1.15.0` firmware (`env:Heltec_v3_companion_radio_usb`), USB-cabled to a Linux host (dev Pi 4 or laptop).
- **Serial logger:** run one of the following in a separate shell to capture *all* UART output including boot banners — do **not** let mesh_bot hold the port during logging tests:
  - `picocom -b 115200 /dev/ttyUSB0 --noreset --imap lfcrlf` — keeps port open, logs to stdout. `--noreset` prevents picocom from toggling DTR/RTS on open.
  - or: `screen /dev/ttyUSB0 115200` (needs explicit C-a k to exit).
- **Kernel log:** `dmesg -Tw` in a third shell, filtered for `usb` / `ttyUSB` / `ch34|cp210`.
- **Reset script:** the `rts_reset.py` (or equivalent) that issues `dtr=False; rts=True; sleep 0.1; rts=False`. Document this script path as we use it.
- **Host USB reset via sysfs:**
  ```
  ID=$(readlink /sys/class/tty/ttyUSB0/device | awk -F/ '{print $(NF-2)}')
  echo 0 > /sys/bus/usb/devices/$ID/authorized
  sleep 1
  echo 1 > /sys/bus/usb/devices/$ID/authorized
  ```
  This renumerates the CP2102 without physical unplug (tests D1 discrimination).
- **Host USB reset via ioctl:** `python3 -c "import usb.core; d=usb.core.find(idVendor=0x10c4); d.reset()"` (CP2102 VID=0x10c4). Alternative to sysfs.

When a test calls for "scope/LA," flag it clearly — we do not assume we have an oscilloscope or logic analyzer; tests requiring them are marked **[optional equipment]**.

## Priorities

**High** — outcome changes the eventual recovery design.
**Medium** — helpful confirmation, won't change design.
**Low** — curiosity / completeness.

---

## T1 — Does the V3 physical RST button reset the ESP32 independently of CP2102 state?

**Hypothesis:** The RST button is wired directly from `CHIP_PU` to GND via the tactile switch — no MCU involvement, so a hung ESP32 cannot prevent it from asserting. If confirmed, mode **A2** is electrically impossible on this board. If disconfirmed, A2 moves up in priority.

**Procedure:**
1. With serial logger attached and no mesh_bot running, confirm baseline: RTS pulse produces the `ESP-ROM:esp32s3-...` banner within ~50 ms.
2. Hold the button for 1 second. Observe banner. Repeat 5×.
3. Disconnect USB. Apply external 5V to the Heltec V3 VIN (if the board has a separate input). Press RST. *Skip this step if we don't have an external power option.*
4. With USB connected, put the CP2102 into `authorized=0` state (host-side USB reset, step not fully released):
   ```
   ID=$(readlink /sys/class/tty/ttyUSB0/device | awk -F/ '{print $(NF-2)}')
   echo 0 > /sys/bus/usb/devices/$ID/authorized
   ```
   Press RST. Does the ESP32 reset (we won't see the banner, but the TX LED pattern or OLED refresh would indicate it)? Then `echo 1` to re-authorize and observe the banner that should now come through.

**Expected observations:**
- H1 (button is direct to EN): step 4 still resets the ESP32 — OLED redraws "Loading…" immediately even with CP2102 deauthorized.
- H2 (button wired through MCU logic, as A2 requires): step 4 has no effect.

**Tools needed:** serial logger, dmesg, sysfs write access.

**Priority:** High — answers a single schematic question that gates hypothesis A2.

---

## T2 — Does RTS pulse produce a ROM banner when the ESP32 is hung in a tight loop?

**Hypothesis:** RTS auto-reset works regardless of ESP32 firmware state (A2 is impossible on a standard circuit).

**Procedure:**
1. Build a debug variant of the companion firmware with a CLI command (or a timer) that calls `while(1);` to produce a deliberate hang. This requires a custom build — flag as a build dependency.
2. Issue the hang, confirm via Pi that commands time out.
3. Issue RTS pulse via script.
4. Observe whether ROM banner appears.

**Expected observations:**
- If banner appears: RTS auto-reset is unconditionally effective. A2 ruled out.
- If banner does not appear: A2 is real on this board; investigate the schematic.

**Tools needed:** custom firmware build, serial logger, RTS reset script.

**Priority:** High — complements T1; together they characterize the reset path.

---

## T3 — Can software-only SX1262 warm-reset recover a deliberately stuck-TX state?

**Hypothesis (B1/B3):** If the SX1262 is stuck mid-TX, the `sx126xResetAGC` SPI sequence at `src/helpers/radiolib/SX126xReset.h:8-37` is sufficient to recover without a VEXT cycle. If true, we can build a recovery path that doesn't need USB unplug. If false, VEXT cycle (or VBUS unplug) is mandatory.

**Procedure:**
1. Build a debug variant with a CLI command that deliberately puts the SX1262 into a stuck state. Candidates for "stuck":
   - Call `startTransmit` then immediately deassert the RX chain such that DIO1 never fires (may require poking the IRQ mask via RadioLib's `setDioIrqParams`).
   - Or, issue a long preamble transmission and yank SPI mid-transaction. Hard to do deterministically from Arduino; consider running the debug build on a separate dev board first.
2. Confirm stuck state via Pi: send `send_chan_msg`; no `MSG_SENT` observed; TX LED stuck on.
3. Invoke a new `radio_recover` CLI command that calls `sx126xResetAGC(radio)` + `radio.std_init(&spi)` + `startReceive()`.
4. Observe whether the radio returns to service.

**Expected observations:**
- H3a (warm reset recovers): normal operation resumes; `send_chan_msg` succeeds. Build-time `MESH_DEBUG` banner about `noise_floor` reappears in log (if enabled).
- H3b (warm reset insufficient): radio remains stuck.
- H3c (warm reset partially recovers — RX works, TX doesn't, or vice versa): important to characterize separately.

**Tools needed:** custom firmware, serial logger.

**Priority:** High — determines whether software-only radio recovery is feasible, which changes the recovery-design space dramatically.

---

## T4 — Does VEXT power-cycle (firmware-initiated) recover from stuck SX1262 states?

**Hypothesis:** Dropping then re-raising `PIN_VEXT_EN=36` power-cycles the SX1262 (and OLED, TCXO). If this recovers all stuck-SX1262 states, we have a non-USB-unplug path.

**Procedure:**
1. Same stuck-state induction as T3.
2. Add firmware CLI command that:
   - Calls `board.periph_power.release(hid)` until VEXT drops low. (Note: `RefCountedDigitalPin` is refcounted; dropping refs to 0 releases the pin. Need to read its implementation to know how to force-drop.)
   - `delay(50)` (ample time for SX1262 power to drain; per SX1262 datasheet the chip's internal state is lost after ~1 ms of unpowered time).
   - Re-acquire VEXT, then call `radio.std_init(&spi)` and `startReceive()`.
3. Observe recovery as in T3.

**Expected observations:**
- H4a (VEXT cycle recovers): robust firmware-initiated recovery is possible; no USB unplug needed.
- H4b (VEXT cycle does not fully recover): there's some latch elsewhere (TCXO, PLL) that needs longer settling, or the issue is in the ESP32's SPI peripheral — investigate.

**Tools needed:** custom firmware; **[optional equipment]** multimeter on VEXT net to confirm it actually drops.

**Priority:** High — pairs with T3. If T3 works we prefer that path; if not, T4 becomes the recovery of last resort before VBUS cycle.

---

## T5 — Can a host-side USB reset (without physical unplug) recover the radio from the observed hang class?

**Hypothesis:** If the failure is in the CP2102 (D1), a host-side `usb.core.reset()` or sysfs `authorized` toggle recovers without a VBUS cycle. If the failure is in the SX1262 (B-family), host-side USB reset does **not** recover it (because VEXT is not cycled by a USB-reset — it's driven by the ESP32 which is unaffected by a CP2102 reset).

**Procedure:**
1. Reproduce the hang. Since it's not currently reproducible on 1.15.0, this test is conditional on getting a repro first. See T7 for induction attempts.
2. Try **in this order** and log which recovers:
   a. RTS pulse (expected: doesn't recover, per original observation).
   b. Physical RST button press (expected: doesn't recover).
   c. `python3 -c "import usb.core; d=usb.core.find(idVendor=0x10c4); d.reset()"` (pyusb CP2102 reset). Watch serial logger for new banner.
   d. sysfs `authorized` toggle. Watch serial logger.
   e. Physical VBUS unplug.

**Expected observations:**
- If (c) or (d) recovers: it's CP2102-side (D1). Recovery design should include a host-side USB reset step before escalating to VBUS cycle.
- If neither (c) nor (d) recovers, but (e) does: it's SX1262-side (B-family) — VEXT cycle is required.
- If (a) or (b) unexpectedly recover: the hang was transient and we got a different failure this time.

**Tools needed:** pyusb installed; sysfs; physical access.

**Priority:** High (conditional on a reproducible hang).

---

## T6 — Does SPIFFS ever silently reformat and lose identity?

**Hypothesis (E1):** `SPIFFS.begin(true)` at `main.cpp:186` will format on corruption. Field corruption could happen from reset-during-write.

**Procedure:**
1. On a known-good node, record `self_info.public_key` via `mc.self_info` after appstart.
2. Exercise lots of RTS resets mid-activity (send channel messages while pulsing RTS at random intervals). Run for several hours.
3. Periodically reconnect and check `self_info.public_key`.
4. Additionally: deliberately corrupt SPIFFS by flashing a partial image to the partition, reboot, observe.

**Expected observations:**
- H6a (pubkey stable): SPIFFS write path is robust; E1 is rare.
- H6b (pubkey changes silently): confirms E1 is real, and we need to add a pubkey-change alert to CivicMesh.

**Tools needed:** serial logger; esptool for flash manipulation in step 4.

**Priority:** Medium — doesn't affect the hang hypothesis but affects long-term deployment reliability.

---

## T7 — Reproduce the original 1.11.0 hang on 1.15.0 (or confirm it's fixed)

**Hypothesis:** If the bug was specific to 1.11.0's `set_radio` interaction (consistent with `mesh_bot.py:320-321` comment and hypothesis B3), it may or may not reproduce on 1.15.0. Testing this separates "still broken" from "no longer a concern."

**Procedure:**
1. On the reflashed V3 running 1.15.0, run mesh_bot with its normal connect flow (including a `set_radio` call if `radio_freq` etc. don't already match).
2. Interrupt mid-`set_radio` with an RTS pulse. Repeat with different timings (immediately after send, 50ms after, 200ms after).
3. Observe whether the radio returns to service after RTS pulse.
4. If 1.15.0 survives, attempt a flash back to 1.11.0 (coordinate with maintainer) and repeat — if 1.11.0 reproduces reliably, hypothesis B3 is confirmed.

**Expected observations:**
- H7a (1.15.0 survives reliably; 1.11.0 breaks reliably): B3 confirmed, 1.15.0 is the fix.
- H7b (1.15.0 also breaks): hang class is not specific to 1.11.0. We need to understand what changed (if anything) and whether the 1.15.0 code path has a similar race.
- H7c (neither reproduces): the hang may have been a one-off hardware/ESD event.

**Tools needed:** ability to flash between 1.11.0 and 1.15.0 (esptool + firmware binaries); serial logger.

**Priority:** High — if 1.15.0 is provably fixed for this class, recovery design scope narrows.

---

## T8 — Does MESH_DEBUG being disabled in companion_radio_usb hide useful diagnostics?

**Hypothesis:** Lines like `MESH_DEBUG_PRINTLN("RadioLibWrapper: error: startReceive(%d)"...)` (`RadioLibWrappers.cpp:101`) would be hugely useful during debugging but are suppressed by `#ifdef MESH_DEBUG`. We need to know whether `MESH_DEBUG=1` breaks anything before turning it on.

**Procedure:**
1. Build a variant with `MESH_DEBUG=1` and `MESH_PACKET_LOGGING=1` *removed from the "DO NOT ENABLE" list* in platformio.ini:147-148.
2. Compare runtime behavior against the stock build: Does it still fit in flash? Is serial throughput still adequate for normal operation (debug prints are significant)? Does it interfere with frame parsing on the Pi (since the Pi's parser tolerates junk with the `0x3E` search)?
3. Run for an hour with heavy mesh traffic. Collect logs.

**Expected observations:**
- H8a (works fine, useful logs): we can enable MESH_DEBUG in a debug variant deployed alongside production for diagnosis.
- H8b (breaks frame parsing on Pi because debug prints overflow buffers or corrupt frame alignment): keep MESH_DEBUG off; find another way to get diagnostics (e.g. a CLI command to dump internal state on demand).
- H8c (flash overflow): disable selectively.

**Tools needed:** custom firmware; serial logger; mesh_bot running.

**Priority:** Medium — unlocks better diagnostics for follow-up tests.

---

## T9 — Liveness ping from the Pi: how often should it run, and what should it do on failure?

**Hypothesis:** Today, mesh_bot cannot detect a silent radio hang (hypothesis D3). Adding a periodic low-cost command like `get_stats_core` with a short timeout would expose silent hangs. Need to characterize: (a) what's the normal round-trip latency, so we can set a sensible timeout? (b) how often can we poll without interfering with mesh traffic?

**Procedure:**
1. On a known-good radio with realistic traffic, measure the latency of `get_stats_core` over an hour — min/max/p50/p99.
2. Vary polling frequencies (10 s, 30 s, 60 s) and observe CPU usage on the Pi and any effect on LoRa TX/RX performance.

**Expected observations:**
- Typical round-trip for `get_stats_core` should be under 100 ms (it's a local command, not over the air). If we see anything higher, that's a signal the serial layer or ESP32 loop is getting congested.
- Polling every 30 s with a 2 s timeout is a reasonable starting point *(inferred; adjust after measurements)*.

**Tools needed:** instrumented mesh_bot or a small standalone script using `meshcore_py`.

**Priority:** Medium — doesn't answer the hang question but sets up the observation framework we need for Phase 3 recovery design.

**Findings:** See `diagnostics/radio/FINDINGS.md` § "T9 — Liveness ping latency characterization".

---

## T10 — Does a pre-reset SPI quiesce prevent the B3 class of hang?

**Hypothesis:** If B3 is caused by the SX1262 being mid-SPI-transaction when the ESP32 resets, adding a firmware-level graceful shutdown (warm sleep + standby before `esp_restart()`) might prevent it. Corollary: a host-side reset script should **not** pulse RTS immediately after a `set_radio` or similar command — it should wait for the command's ack first.

**Procedure:**
1. On 1.15.0 (or whatever version reproduces the hang in T7), exercise repeated `set_radio` + RTS cycles with two variants of host-side script:
   - Variant A: pulse RTS immediately after calling `set_radio`.
   - Variant B: pulse RTS at least 500 ms after `set_radio` completion.
2. Count hang rate per 100 cycles.

**Expected observations:**
- If Variant B's hang rate is substantially lower than Variant A's: the hang is timing-dependent in the way B3 predicts, and a simple host-side "don't reset near command boundaries" heuristic is a partial mitigation.
- If rates are equal: timing isn't the trigger; the root cause is elsewhere (B1, B2, or D1).

**Tools needed:** reproducer from T7; scripted reset variants.

**Priority:** Medium.

---

## T11 — Characterize what `dmesg` shows during each failure class **[optional equipment: none, just observation discipline]**

**Hypothesis:** Different failure classes produce distinct `dmesg` signatures. Building a catalogue will let future diagnosis start from dmesg output alone.

**Procedure:**
Run `dmesg -Tw` continuously during each of:
1. Normal operation (baseline).
2. Deliberate USB unplug + replug.
3. RTS reset pulse (clean).
4. Host-side sysfs authorize-toggle.
5. Induced ESP32 hang (from T2, if we have that firmware).
6. Induced SX1262 stuck state (from T3, if we have that firmware).

Record the dmesg lines for each.

**Expected observations:** a reference card of "what dmesg says when X happens" that we can include in field-support docs.

**Priority:** Low (nice-to-have but high value once built).

---

## T12 — Is the observed `rst:0x1 (POWERON)` banner on RTS reset definitely from an RTS-induced CHIP_PU assertion? **[optional equipment: oscilloscope]**

**Hypothesis:** The ROM banner text "POWERON" is reported for any reset via the `CHIP_PU` pin, including the transistor-initiated one. Scope verification would rule out alternate paths (e.g., some other reset source firing coincidentally).

**Procedure:**
1. With scope channel 1 on `CHIP_PU` (requires hooking it on the V3 — small test point or kluge wire), channel 2 on RTS pin of the CP2102.
2. Issue RTS pulse. Capture both waveforms.

**Expected observations:**
- Clean time-correlated fall of `CHIP_PU` a few ms after RTS goes high, then rise shortly after RTS drops.

**Tools needed:** **[optional equipment]** oscilloscope, test-point access. Skip if unavailable.

**Priority:** Low — a higher-confidence confirmation that doesn't change recovery design.

---

## T13 — Firmware logs its own boot reset reason on every boot

**Hypothesis:** Future silent-recovery events can be classified if the firmware always prints `esp_reset_reason()` on boot. This is a tiny code change; the diagnostic value is high.

**Procedure:**
1. Add one `Serial.printf("reset_reason: %d\n", (int)esp_reset_reason());` line to `setup()` immediately after `Serial.begin(115200)`.
2. Deploy. Over the course of other testing, collect reset reasons from every boot event.

**Expected observations:**
- Build a histogram: POWERON (1) from our RTS tool, POWERON (1) from VBUS unplug (cannot distinguish these two electrically), SW (3) from `board.reboot()`, PANIC (4) if any panics, etc.
- If we see unexpected reset reasons (WDT, brownout), that's a signal worth investigating.

**Tools needed:** one-line firmware change, build access.

**Priority:** High — cheap, immediately useful for every subsequent test that needs to know what kind of reset happened.

---

## Test-to-question matrix

| Open question (source) | Test |
|---|---|
| Does RST button defeat A2? (boot-and-reset Q2, our-hang A2) | T1 |
| Can RTS defeat A2? (boot-and-reset Q1) | T2 |
| Can software-only SX1262 recovery work? | T3 |
| Can firmware-initiated VEXT cycle recover? | T4 |
| CP2102 reset vs SX1262 reset: which fits the observed hang? | T5 |
| Can SPIFFS reformat silently lose identity? (boot-and-reset Q3, E1) | T6 |
| Was the 1.11.0 hang fixed in 1.15.0? | T7 |
| Is MESH_DEBUG safe to enable for diagnosis? (boot-and-reset Q4) | T8 |
| What are realistic liveness-ping parameters? | T9 |
| Does timing-isolated RTS avoid B3? | T10 |
| dmesg signatures per failure class | T11 |
| Is POWERON banner really from `CHIP_PU`? | T12 (optional) |
| Reset-reason logging for future tests | T13 |

## Equipment availability caveats

Every test uses a serial logger, which we assume we have (it's just `picocom` or equivalent). Tests flagged **[optional equipment]** (T4 partial, T11 none, T12 full) require hardware beyond that — flag those to the operator before attempting.

Tests requiring firmware builds (T2, T3, T4, T8, T13) require the ability to rebuild and reflash the companion firmware. Coordinate with whoever maintains the CivicMesh radio build process.
