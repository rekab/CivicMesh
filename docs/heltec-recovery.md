# Heltec V3 recovery reference

Hardware reference for silent-hang detection and recovery (CIV-41).
Written for Pi Zero 2W deployment target.

**Implementation:** see `docs/recovery.md` for the software design,
configuration, state machine, and observability.

## The three chips

The Heltec V3 has three independent chips on separate power domains. Understanding which chip is stuck determines which recovery action can fix it.

**ESP32-S3** — runs the MeshCore companion firmware. Receives commands from the Pi over serial, manages the LoRa radio over SPI, handles channel join/leave, message send/receive. When this hangs, serial commands time out but the port stays open.

**SX1262** — the LoRa radio chip. Controlled by the ESP32 over SPI. Has no hardware reset pin on the V3 (`P_LORA_RESET=RADIOLIB_NC` in the MeshCore firmware config). The only ways to reset it are: (1) a VEXT power cycle, where the ESP32 firmware cuts power on GPIO36 to the 3.3V rail feeding the SX1262, or (2) physically cutting power to the whole board. A stuck SX1262 is the hardest failure to recover from in software.

**CP2102** — the USB-to-serial bridge. Translates between the Pi's USB and the ESP32's UART. Also carries the RTS/DTR signals used for auto-reset. If this chip hangs, the serial port appears open on the Pi side but no data or control signals pass through. RTS reset pulses can't reach the ESP32 because they route through this chip.

## VEXT power cycle

VEXT is the 3.3V power rail controlled by ESP32 GPIO36 (`PIN_VEXT_EN`). It feeds the SX1262, the OLED display, and the TCXO. The firmware can toggle this pin to power-cycle the radio without resetting the ESP32 itself.

This is the only software-controllable path that resets the SX1262. However:

- It requires the ESP32 firmware to be running and responsive — the Pi sends a serial command, and the firmware executes the power cycle internally.
- If the ESP32 is also hung, nobody is home to execute the command.

**Status: NOT AVAILABLE.** Confirmed by firmware code review: MeshCore companion firmware does not expose a VEXT power-cycle command. The firmware has 60+ serial commands (`CMD_*` codes in `MyMesh.cpp`), none of which toggle VEXT. The plumbing exists internally — `HeltecV3Board::periph_power` is a `RefCountedDigitalPin` on `PIN_VEXT_EN=36` — but no serial command handler drives it. `CMD_REBOOT` (code 19) calls `esp_restart()`, which resets the ESP32 core only and does not touch VEXT. The `meshcore_py` library has no references to VEXT or radio power cycling.

Adding a `CMD_POWER_CYCLE_RADIO` that does `periph_power.release(); delay(50); periph_power.claim(); radio_init();` would be straightforward in firmware, but requires a custom MeshCore build. This is currently deferred to post-Toorcamp (CIV-44) but may need to be reconsidered given the implications below.

## Pi Zero 2W USB constraints

The Pi Zero 2W has a single USB data port using the DWC2 OTG controller. Unlike the Pi 3B/4B which have an internal USB hub chip with power switching circuitry, the Zero 2W connects the DWC2 controller directly to the port with VBUS tied to the 5V rail.

Consequences:

- **`uhubctl` does not support the Pi Zero 2W.** There is no internal hub to control.
- **`sysfs authorized=0/1` will logically disconnect the device** (CP2102 re-enumerates, `/dev/ttyUSB0` disappears and reappears) **but VBUS stays powered.** The ESP32 and SX1262 never lose power.
- **`pyusb device.reset()` also does a logical disconnect only.** Same effect as sysfs — CP2102 resets, but no power interruption to the ESP32 or SX1262.
- On the Zero 2W, pyusb reset and sysfs authorized toggle are functionally equivalent. Both reset the CP2102 and re-enumerate, but neither cuts power.

**The only way to fully power-cycle all three chips on the Zero 2W is physical USB unplug or full power-cycle of the Pi's own power supply (pulling the battery).** A software `reboot` command likely does NOT cut VBUS — the Pi's power management chip keeps the 5V rail up during a reboot, so the Heltec stays powered throughout.

## Recovery ladder

Ordered from cheapest/fastest to most disruptive. Each step targets specific chip(s).

### Step 1: RTS serial pulse

Pi sends DTR=low, RTS=high, wait 100ms, RTS=low over the serial port. This toggles the auto-reset transistor on the Heltec, pulling the ESP32's CHIP_PU (enable) pin low. The ESP32 reboots and re-initializes its firmware, including re-running `radio_init()`.

- **Resets:** ESP32 only.
- **Does not reset:** SX1262 (no reset wire), CP2102.
- **Recovers:** ESP32 firmware hang (A-family). Also recovers stuck SX1262 *if* the firmware's `radio_init()` successfully re-initializes the radio after reboot (depends on whether the SX1262's SPI interface is responsive).
- **Fails when:** CP2102 is hung (RTS signal can't pass through), or SX1262 is in a state that `radio_init()` can't recover from.
- **Side effects:** serial port stays open, `/dev/ttyUSB0` unchanged. Cheapest possible reset.

### Step 2: GPIO EN toggle (requires CIV-30 hardware mod)

Pi drives a GPIO pin connected directly to the Heltec's CHIP_PU (EN) pin via a soldered jumper wire. Electrically identical to step 1, but bypasses the CP2102.

- **Resets:** ESP32 only.
- **Does not reset:** SX1262, CP2102.
- **Recovers:** everything step 1 recovers, plus the case where the CP2102 bridge is hung.
- **Fails when:** SX1262 is stuck in a state that `radio_init()` can't clear.
- **Side effects:** same as step 1 — serial port stays up.
- **Availability:** only if the GPIO wire is physically soldered. Config should specify the pin number; recovery code should skip this step if unconfigured.

### Step 3: VEXT power cycle (requires firmware support)

Pi sends a serial command asking the ESP32 firmware to toggle GPIO36, cutting then restoring the 3.3V rail to the SX1262.

- **Resets:** SX1262 only.
- **Does not reset:** ESP32 (stays running), CP2102.
- **Recovers:** stuck SX1262 radio (B-family) — the one failure mode that steps 1 and 2 cannot reach.
- **Fails when:** ESP32 firmware is also hung (can't execute the command), or CP2102 is hung (command can't reach the ESP32).
- **Side effects:** serial port stays up. OLED and TCXO also lose power briefly (same rail). Radio needs re-initialization after power is restored.
- **Availability:** **NOT CURRENTLY AVAILABLE.** No serial command exists in MeshCore companion firmware. Requires a firmware patch adding `CMD_POWER_CYCLE_RADIO`. See VEXT section above for details.

### Step 4: USB logical reset (pyusb or sysfs)

Pi tells the kernel to re-enumerate the USB device. On the Zero 2W, this does NOT cut VBUS power — it only resets the CP2102's logical state and causes `/dev/ttyUSB0` to disappear and reappear.

- **Resets:** CP2102 only (on Zero 2W).
- **Does not reset:** ESP32, SX1262 (VBUS stays powered on Zero 2W).
- **Recovers:** CP2102 bridge hung, stale serial port state.
- **Fails when:** the problem is the ESP32 or SX1262, not the bridge.
- **Side effects:** `/dev/ttyUSB0` disappears and reappears. `mesh_bot.py` must handle port loss and reconnection. May reappear as a different device number.
- **Note:** on Pi 3B/4B (which have a USB hub chip), this *might* also cut VBUS, which would reset the ESP32 and SX1262 too. On Zero 2W, it does not.

### Step 5: Pi reboot

Systemd watchdog or explicit `reboot` command. Restarts all services and reinitializes the USB host controller.

- **Resets:** ESP32 and CP2102 (USB host controller reinitializes, triggering re-enumeration).
- **Probably does NOT reset:** SX1262. The Pi's power management chip keeps the 5V rail up during a software reboot, so VBUS stays powered, so the Heltec's SX1262 never loses its 3.3V VEXT supply. **This needs empirical verification** — plug in the USB power meter, run `sudo reboot`, and watch whether current drops to zero momentarily.
- **Side effects:** all services restart, WiFi AP goes down briefly, connected clients are disconnected. SQLite must survive unclean shutdown (WAL mode helps).
- **If VBUS does stay up during reboot:** a stuck SX1262 survives a Pi reboot. The only recovery is physical power interruption (unplug USB cable, or pull the battery). This makes the VEXT firmware command (step 3) critically important.

### Step 6: "Needs human"

If the ladder has been exhausted (or the Pi has rebooted and the problem persists), surface a visible alert on the captive portal and in logs.

- **When:** all automated recovery has failed, or the node is in a reboot loop.
- **Surface:** captive portal banner, structured log entry, admin CLI status.
- **Not terminal:** as of CIV-41 (2026-04-21), `RecoveryController` does not stop trying in NEEDS_HUMAN. It retries the full ladder on exponential backoff (60s → 2 min → 4 min → … → 1 hour cap). The process never exits. NEEDS_HUMAN means "automated recovery has not worked yet and a human should look," not "the system has given up."

## Implementation status (2026-04-21)

CIV-41 implemented automated detection and step 1 (RTS pulse) in `recovery.py`. Detection uses two independent triggers: a liveness task that polls `get_stats_core` every 30s (3 consecutive timeouts ≈ 90s worst-case) and an outbox-failure trigger (3 consecutive `send_chan_msg` errors). Both feed a single `RecoveryController` that owns the `mesh_client` reference and runs the ladder.

| Step | Status |
|------|--------|
| 1 — RTS pulse | **Implemented.** Single rung in the recovery ladder. |
| 2 — GPIO EN toggle | Not implemented. Requires CIV-30 hardware mod (soldered jumper wire). |
| 3 — VEXT power cycle | Not implemented. Requires firmware patch (CIV-44). |
| 4 — USB logical reset | Not implemented. Removed from scope — pyusb `device.reset()` caused ttyUSB renaming and 30-minute unreachability on test hardware. |
| 5 — Pi reboot | Not automated. A stuck radio that survives RTS enters NEEDS_HUMAN; operator intervention (process restart, USB unplug, or battery swap) is the fallback. |
| 6 — Needs human | **Implemented** as the NEEDS_HUMAN state with exponential-backoff retry (see step 6 description above). |

See `docs/recovery.md` for the software design, state machine, observability, and operational thresholds (swap criteria for Toorcamp).

## What the ladder cannot reach

The structural gap is worse than it first appears: **on the Pi Zero 2W, there is currently no software-only path that resets the SX1262.**

- Steps 1-2 reset the ESP32 but not the radio. The radio *may* recover if `radio_init()` can re-initialize the SX1262 over SPI after the ESP32 reboots — but if the SX1262 is in a state where SPI commands don't work, this fails.
- Step 3 (VEXT power cycle) is the only action that resets the SX1262 without cutting board power — but it doesn't exist yet. No serial command for it in the firmware.
- Step 4 resets the CP2102 only. VBUS stays up on Zero 2W.
- Step 5 (Pi reboot) probably doesn't cut VBUS either, so the SX1262 likely survives it.

**Worst case: a stuck SX1262 requires physical intervention** — someone walks up and unplugs the USB cable or swaps the battery pack. At Toorcamp, this means the swap schedule doubles as the recovery schedule.

**Mitigations (in order of impact):**

1. **Add `CMD_POWER_CYCLE_RADIO` to MeshCore firmware** — turns step 3 from "not available" to the primary SX1262 recovery path. This is straightforward (~10 lines of firmware code) but requires building and flashing custom firmware. Currently deferred to post-Toorcamp (CIV-44), but this analysis suggests it should be reconsidered.
2. **Test whether `radio_init()` after RTS reset recovers common SX1262 stuck states** (CIV-40). If it does, step 1 may cover most real-world radio failures even without VEXT control. This is the most important empirical question.
3. **Battery swap schedule as implicit recovery.** If battery swaps happen every ~36 hours and require unplugging USB, every swap is a full power cycle. A stuck radio can't persist longer than one swap interval.
4. **Nightly 4am cron reboot + watchdog** still helps recover ESP32 and CP2102 hangs, even if it can't reach the SX1262.
5. **Interpose a downstream powered USB hub with per-port power control** (uhubctl-compatible, e.g., certain small OEM hubs). Pi Zero 2W → hub → Heltec V3. `uhubctl -a off -p <port>; sleep 1; uhubctl -a on -p <port>` then drops VBUS at the downstream port while leaving the Zero 2W's root port alone. This is the only fully automated software-driven true VBUS cycle possible on the Zero 2W, and it works regardless of which of the three chips is stuck. Cost: one BOM line item per deployment; modest current overhead on the Pi's upstream port.

**Rejected alternatives:**

- **Adding VEXT power-cycle logic inside `radio_init()` itself** (so every ESP32 reset transitively power-cycles the SX1262). Self-healing on every boot, no new command, variant-local. Rejected because it pollutes the codepath that millions of non-CivicMesh V3 devices run on every boot with recovery logic that should only run in rare failure scenarios. A discrete `CMD_POWER_CYCLE_RADIO` (mitigation 1) keeps the recovery mechanism opt-in and off the hot path.

## Recovery ladder summary

| Step | Action | ESP32 | SX1262 | CP2102 | Port stable? |
|------|--------|-------|--------|--------|-------------|
| 1 | RTS pulse | resets | maybe via radio_init | no | yes |
| 2 | GPIO EN toggle | resets | maybe via radio_init | no | yes |
| 3 | VEXT power cycle | no | resets | no | yes |
| 4 | USB logical reset | no | no | resets | no — port disappears |
| 5 | Pi reboot | resets | **probably not** | resets | no — everything restarts |
| 6 | Needs human (unplug) | resets | resets | resets | no — full power cycle |

## Open questions

- **Does VBUS drop during a Pi reboot?** Empirical test: USB power meter inline, `sudo reboot`, watch current. If current drops to zero momentarily, Pi reboot does reset the SX1262 and the gap is smaller than documented here. If it stays up, the gap is real.
- Does `radio_init()` after an RTS/EN reset reliably recover a stuck SX1262, or does it depend on the SX1262's SPI state? (CIV-40 should answer this.)
- Should the VEXT firmware command be added before Toorcamp? It's ~10 lines of firmware code but requires a custom MeshCore build and reflash of all Heltecs.
- Should step 2 (GPIO) and step 3 (VEXT) be attempted in combination — reset ESP32 via GPIO, then immediately send VEXT cycle command to the freshly-booted firmware?
- On the Zero 2W, is pyusb `device.reset()` functionally identical to sysfs `authorized=0/1`? (Expected: yes, both are logical-only.)
