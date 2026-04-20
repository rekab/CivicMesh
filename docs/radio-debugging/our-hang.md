# Our hang — evidence and hypotheses

The concrete failure we observed (firmware 1.11.0 at the time; radio since reflashed to 1.15.0; not currently reproducible):

1. **Evidence-1**: An RTS reset pulse using `dtr=False; rts=True; sleep 0.1; rts=False` **did not** recover the radio.
2. **Evidence-2**: The physical RST button on the V3 board **did not** recover it.
3. **Evidence-3**: Unplugging USB for several seconds and re-plugging **did** recover it.
4. **Evidence-4** (missing): The reset reason on recovery was not captured, so we don't know what the chip's first post-recovery `esp_reset_reason()` returned.

The reset-domain table in [`boot-and-reset.md § 2`](boot-and-reset.md#2-reset-domains) is the anchor for the analysis below. Note that Evidence-1 and Evidence-2 are a strong joint signal: both exercise the same electrical endpoint (`CHIP_PU`), so if both failed to recover while USB unplug succeeded, the failure is almost certainly *not* in the ESP32 digital core alone — it's either in a component whose state is not cleared by asserting `CHIP_PU` (SX1262, CP2102, VEXT rail) or in the reset path itself (something preventing the button/transistor from asserting EN).

## Per-mode analysis

Each row: is the mode consistent with (✓) / ruled out by (✗) / ambiguous given (?) the observed evidence? If surviving, what does it predict about a successful repro?

| ID | Mode | Fit | Reasoning + prediction for repro |
|---|---|---|---|
| A1 | ESP32 hang | ✗ | Ruled out. Both RTS and RST button assert `CHIP_PU`, which unconditionally resets the ESP32 core regardless of what the firmware is doing. A pure ESP32 hang would have been recovered by either. |
| A2 | Hang defeating auto-reset path | ? | Consistent with Evidence-1 alone, but **Evidence-2 (RST button) should not be defeatable by ESP32 state** on a standard circuit where the button shorts EN to GND directly. If A2 is the actual cause, it requires the RST button to be wired through some logic the ESP32 could hold — needs schematic verification. **Prediction if repro:** a serial logger watching UART during the RTS pulse would show no ROM banner at all; `dmesg -w` would show no USB re-enumeration (since only the ESP32 would reset, and CP2102 is separately powered). If we could also probe `CHIP_PU` with a scope we'd see it stay high despite the transistor driving low. |
| A3 | Panic → reboot | ✗ | A panic self-resets within ~1-2 s and produces a ROM banner on the UART. The reported failure was "unresponsive" for long enough to try multiple recovery mechanisms — inconsistent with a single panic reboot. Also, a successful panic reset would have been observable on the Pi as a burst of backtrace text. |
| A4 | Panic reboot loop | ? | Possible but the Pi would have seen repeated panic banners on the serial port. The report doesn't say bytes were flowing — if they weren't, A4 is ruled out; if they were (but looked like junk), A4 is consistent. **Prediction if repro:** `cat /dev/ttyUSB0` during the stuck period shows repeated `Guru Meditation` + `rst:0xA (PANIC)` banners. |
| A5 / A6 | Task/Int WDT | ✗ | Both self-recover (the whole point of a WDT). If they fired, the chip would have come back on its own and RTS wouldn't have been needed. |
| A7 | Brownout | ✗ | Same self-recovery argument. Also inconsistent with the chip running long enough to appear unresponsive to the operator. |
| **B1** | **SX1262 stuck in TX** | **✓** | Consistent with all three pieces of evidence. RTS and RST reset the ESP32; the ESP32 re-runs `radio_init` on boot; but on V3, `P_LORA_RESET=RADIOLIB_NC` so `std_init` cannot hardware-reset the SX1262. If the SX1262 is already in a state that doesn't respond to the SPI commands inside `std_init`, `std_init` returns false, firmware prints `ERROR: radio init failed:` and halts (`main.cpp:126` → `halt();`). The Pi sees one error line then silence. Subsequent RTS resets reproduce this exact sequence. Only VEXT cycle (via VBUS unplug) clears the SX1262 state. **Prediction if repro:** on the Pi, after an RTS pulse you'd see the ROM banner followed within ~100 ms by the `ERROR: radio init failed:` string, then nothing. `dmesg -w` would show no USB re-enumeration (CP2102 stays alive). |
| **B2** | **SX1262 stuck in RX** | **✓** | Same mechanism as B1. The firmware's `std_init` starts with `SX1262::begin(...)` which issues SPI commands assuming the chip is in a responsive standby. If the chip is stuck in a state where those SPI commands fail (-707/-706 retry fallback notwithstanding), same outcome: `ERROR: radio init failed:` then halt. Same prediction. |
| **B3** | **SX1262 undefined after partial SPI transaction** | **✓ (strongest fit)** | The 1.11.0 → 1.15.0 reflash point is suggestive: if the bug was that some SPI sequence on 1.11.0 occasionally left the SX1262 mid-transaction (e.g., `set_radio` mid-calibration getting interrupted by a subsequent RTS pulse), the SX1262 would be stuck until VEXT cycle. That matches "RTS doesn't recover, only unplug does." Note also that CivicMesh's own notes at `mesh_bot.py:320-321` mention "on some firmware (e.g. v1.11.0) set_radio breaks the session." — independent evidence that 1.11.0 had a bad interaction with mid-setup SPI. **Prediction if repro (would require rolling back to 1.11.0 or finding a similar repro on 1.15.0):** the failure occurs reliably after a `set_radio` call that's interrupted by a reset. |
| B4 | VEXT brownout | ? | Possible but speculative — requires a transient power event we have no evidence for. Consistent with the "only USB unplug recovers" observation (VBUS cycle re-powers VEXT). Lower prior than B1/B2/B3. |
| C1 | Pi→radio framing desync | ✗ | Framing desync in the firmware's `checkRecvFrame` state machine doesn't prevent the Pi from receiving data from the radio. Also, a fresh ESP32 reset (RTS pulse) resets `_state = RECV_STATE_IDLE` at `ArduinoSerialInterface.cpp:10`, clearing any desync. So RTS should have recovered a pure framing desync. |
| C2 | Radio→Pi framing desync | ✗ | Same: reset on either side resets the parser. Doesn't explain why USB unplug was specifically needed. |
| **D1** | **CP2102 bridge hung** | **✓** | Strong fit. RTS and RST both route through the CP2102's DTR/RTS output pins, so if the CP2102 itself is hung, those control-line outputs may also be stuck and the ESP32 never sees the reset assertion. USB unplug resets the CP2102 by cutting its power. **Prediction if repro:** `dmesg -w` during the stuck period may show CTS/DTR timeouts. If you `echo 0 > /sys/bus/usb/devices/<N>/authorized; echo 1 > .../authorized` instead of physical unplug, the radio recovers — this would confirm D1 (CP2102 reset) without invoking B-family hypotheses. A further discriminator: use a `pyusb` reset (`dev.reset()`) to renumerate *without* cutting VBUS — if that recovers, it's D1; if it doesn't, it's B-family. |
| D2 | Stale ttyUSB | ✗ | A stale FD is process-local; opening the port in a different shell would work. Unplug isn't necessary to recover. Doesn't fit Evidence-3. |
| **D3** | **Silent hang, umbrella** | **✓** | D3 is always consistent because it's the Pi-side surface of any radio-side hang. Doesn't distinguish between candidates. |
| E1 | SPIFFS reformat | ✗ | Reformatting at boot is invisible to the "recovers on unplug" test because any reset (RTS, RST, unplug) triggers it equally. Doesn't explain why only unplug recovered. |
| E2 | Partial prefs loss | ✗ | Same — doesn't involve a component outside the ESP32. |
| F1/F2 | BLE / WiFi wedged | ✗ | Not compiled in for the USB build. |

## Surviving hypotheses, in priority order

1. **B3 — SX1262 undefined after partial SPI transaction**, precipitated by a mid-setup interruption on 1.11.0 firmware. Strongest fit: explains all three evidence items, aligns with existing CivicMesh-repo comment about 1.11.0 / `set_radio`, matches the "V3 has no SX1262 reset pin" architectural constraint (`P_LORA_RESET=RADIOLIB_NC` in `variants/heltec_v3/platformio.ini:13`).

2. **B1 / B2 — SX1262 stuck TX or RX** not necessarily tied to interrupted setup. Same recovery mechanism as B3 (only VEXT cycle clears it), so indistinguishable from Evidence-3 alone.

3. **D1 — CP2102 bridge hung**. Different mechanism (USB bridge, not radio). Distinguishable from B-family by whether a **host-side USB reset** (pyusb `reset()` or `sysfs authorized` toggle) recovers without a VBUS cycle.

4. **A2 — ESP32 firmware hang defeating the reset path**. Only survives if the V3 RST button is *not* wired straight to EN — needs schematic verification.

5. **A4 — Panic reboot loop**. Survives only if the Pi was receiving a stream of panic text that wasn't characterized as such at the time.

## Contradictions with prior claims

One point in the original prompt deserves a flag:

- The prompt says the RTS pulse "confirms the standard ESP32 auto-reset transistor circuit is present and functional on the V3." That's true for the working case, but note that **the `rst:0x1 (POWERON)` banner only indicates that once the reset was asserted, the chip came up cleanly** — it does not confirm that the transistor circuit successfully asserts `CHIP_PU` under all conditions, only that it did in that particular moment. In other words: Evidence-1 (RTS-didn't-recover during the hang) is compatible with "the transistor circuit is functional when the CP2102 is healthy but inert when the CP2102 is hung." The "standard auto-reset is present" claim should be read as a positive existence result, not a universal claim about reliability.

- The prompt refers to "pin 7 is connected" without elaboration — I don't have context on which pin this refers to. If it refers to `P_LORA_BUSY=13` (GPIO13) or `P_LORA_DIO_1=14`, no contradiction; but I'd want to verify before taking as ground truth.

## What the source disagrees with

- The standard "RTS resets ESP32 cleanly" assumption is only true for the *ESP32 core*. On V3 it specifically does not reset the SX1262 (`P_LORA_RESET=RADIOLIB_NC`). A debugging narrative that conflates "RTS reset" with "radio reset" will be wrong for SX1262 failures, and this is material for our hang because the surviving hypotheses B1/B2/B3 all share this property.

- The CivicMesh repo's `mesh_bot.py:320-321` already notes that `set_radio` broke 1.11.0 sessions, which dovetails directly with the B3 hypothesis. That comment predates this analysis and provides independent support for B3 being a real failure mode on 1.11.0.
