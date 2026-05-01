# Failure modes

Enumerated failure modes with: Pi-side symptom, radio-side symptom (OLED / LEDs / RF), which resets in the [reset-domain table](boot-and-reset.md#2-reset-domains) recover each, signals observable on the Pi, and a likelihood tag.

Likelihood legend — rough field bucket, not a measurement:
**H** (high) — expected to see over weeks of deployment.
**M** (medium) — plausible once per deployment window.
**L** (low) — possible but requires specific conditions.
**?** — cannot classify without data.

Each mode has an ID (§A–§E families) used by [`our-hang.md`](our-hang.md) and [`test-plan.md`](test-plan.md).

## A — ESP32 firmware

### A1. ESP32 firmware hang (infinite loop / logic deadlock in `loop()`)

Loop code enters an infinite `while` or equivalent. No task WDT is armed on the Arduino main task *(inferred from absence of any `esp_task_wdt_add` calls in the firmware tree — grep for the symbol returns zero matches)*, so the chip spins indefinitely without resetting itself.

- **Pi symptom:** serial port stays open; all commands time out (`DEFAULT_TIMEOUT = 5.0` in `meshcore_py/src/meshcore/commands/base.py:61`, returning `Event(EventType.ERROR, {"reason": "timeout"})`). No `connection_lost` on the Pi because the USB bridge is still up.
- **Radio symptom:** OLED frozen on last frame (ui_task can't advance). TX LED stuck in last state. No RF out.
- **Recovery:** RTS auto-reset pulse (assertion path through the transistor is independent of ESP32 firmware). RST button. VBUS cycle. `esp_restart()` is not reachable because firmware is hung.
- **Detection:** any command times out; no unsolicited frames arrive; `get_stats_core` times out.
- **Likelihood:** M — tight loops in `MyMesh::loop` around contact iteration and radio state machine are plausible hang sites.

### A2. ESP32 firmware hang holding GPIO in a state that defeats RTS auto-reset

Sub-case of A1 where the hang happens with ESP32-controlled pins influencing the reset circuit. On a standard two-transistor auto-reset the RTS/DTR signals go to the bases of transistors that pull EN/IO0; the ESP32 cannot hold those pins, so RTS should always work. However:

- If an ESP32 GPIO is accidentally driving a pin that also connects to EN/IO0 through a rail or protection diode, a held-high state could fight the transistor pull. *(Inferred from general ESP32 hardware knowledge; the V3 schematic would disconfirm or confirm this.)*
- If the CP2102 itself is hung (see §D1) while the ESP32 is fine, RTS signal never makes it to the transistor.

- **Pi symptom:** RTS toggle returns no banner, everything else silent.
- **Radio symptom:** same as A1.
- **Recovery:** RST button still works if wired straight to EN. VBUS cycle works unconditionally.
- **Detection:** RTS pulse does not produce the ROM banner on the serial port.
- **Likelihood:** L (for genuine RTS-defeat) / **M** for the CP2102-hang sibling (§D1).

### A3. ESP32 crash / panic → reboot (no loop)

Unhandled exception triggers ESP-IDF's panic handler which prints a backtrace and then resets with reason `ESP_RST_PANIC` (code 4). This is the "ESP32 recovers itself" case.

- **Pi symptom:** `connection_lost` callback **may or may not fire** depending on whether the USB bridge re-enumerated. CP2102 stays enumerated across an ESP32 reset (its power is not cut). So pyserial sees continuous bytes: the panic backtrace on UART followed by the ROM banner. Because the library treats the banner as "junk before next frame" (it searches for `0x3E` in `serial_cx.py:79`), the junk is skipped. After reset, in-flight commands on the Pi time out (5s default). Future commands work if the Pi retries.
- **Radio symptom:** brief OLED glitch / restart of "Loading…" text. TX LED off during boot.
- **Recovery:** Self-recovers. If the panic repeats (e.g. same exception on boot), the device enters a **panic reboot loop** → A4.
- **Detection:** panic signature strings on UART (`Guru Meditation Error`, `Backtrace:`), followed by ROM banner within 1-2 s. `esp_reset_reason()` on next boot returns `ESP_RST_PANIC`.
- **Likelihood:** M.

### A4. ESP32 panic reboot loop

Panic recurs on boot — e.g., corrupt prefs in SPIFFS causing `loadPrefs` to dereference bad data, or a corrupt identity. The firmware does not validate prefs beyond `constrain()` calls on individual fields (MyMesh.cpp:919-927), which protects against out-of-range numerics but not against structural corruption of the file itself.

- **Pi symptom:** serial port repeatedly emits panic-banner-panic-banner. Commands never complete.
- **Radio symptom:** OLED repeatedly shows "Loading…".
- **Recovery:** VBUS cycle + `SPIFFS.format()` via build-time override — not something we can do from the Pi. In-field recovery requires flashing or wiping NVS.
- **Detection:** panic strings appear in a loop.
- **Likelihood:** L.

### A5. Task watchdog timeout

Not normally armed on the main task. Could fire if some ESP-IDF subsystem (WiFi, BLE) has its own monitor and that subsystem is loaded. In the companion_radio_usb build, WiFi and BLE are not compiled in (platformio.ini:139-156 shows no WIFI_SSID, no BLE_PIN_CODE), so this should be effectively impossible.

- **Reset-reason:** `ESP_RST_TASK_WDT`.
- **Recovery:** self-resets.
- **Likelihood:** L.

### A6. Interrupt watchdog timeout

ESP-IDF's IWDT fires if an ISR holds off scheduling for >300 ms. RadioLib's DIO1 ISR (`setFlag` in `src/helpers/radiolib/RadioLibWrappers.cpp:22-25`) is trivially short. Unlikely to trigger unless a framework-level ISR misbehaves.

- **Reset-reason:** `ESP_RST_INT_WDT`.
- **Recovery:** self-resets.
- **Likelihood:** L.

### A7. Brownout

Supply voltage falls below threshold (default ~2.44V on S3). On a Pi Zero 2W powered from a wall adapter, this should be rare unless the Pi's USB port sags during WiFi TX bursts or another USB device draws current.

- **Pi symptom:** `connection_lost` likely fires because the CP2102 also loses power (it shares the same 3.3V domain from the USB-to-3.3V regulator on V3, *inferred — I have not seen the V3 regulator schematic*). When USB renumerates, `/dev/ttyUSB0` reappears possibly with the same number.
- **Reset-reason on next boot:** `ESP_RST_BROWNOUT`.
- **Recovery:** self-resets once power is stable.
- **Likelihood:** L for wall-powered Pi; M if ever battery/USB-hub powered.

## B — SX1262 radio

> **In-band recovery is not available for B-family failures.** Every mode below that lists "VEXT cycle" as a recovery path has no corresponding serial command on V3 companion firmware v1.15.0. The firmware exposes `CMD_REBOOT` (`MyMesh.cpp:1439-1443`), which calls `board.reboot()` → `esp_restart()` on ESP32 (`src/helpers/ESP32Board.h:124-126`) — a software reset of the ESP32 core only. `PIN_VEXT_EN` (GPIO36) is not toggled by reboot, so SX1262 state survives the ESP32 reset. There is no `CMD_POWER_CYCLE_RADIO`, no command that drives `HeltecV3Board::periph_power` (`variants/heltec_v3/HeltecV3Board.h:27`), and nothing in meshcore_py that can request one. The only ways to VEXT-cycle the SX1262 today are: (a) VBUS disconnect (physical unplug or host-side USB port disable), or (b) a firmware change adding a command that does `periph_power.release(); delay; periph_power.claim();` and re-runs `radio_init()`. Option (b) is noted as a design option for the later recovery phase; it is not implemented in the analyzed firmware.

### B1. SX1262 stuck in TX state

After `startTransmit()`, the SX1262's DIO1 interrupt fails to fire (either because the IRQ mask is misconfigured, the packet never actually leaves the chip, or the chip enters an undefined substate). State machine stays `STATE_TX_WAIT` in `RadioLibWrappers.cpp:147` forever — `isSendComplete()` never returns true (`RadioLibWrappers.cpp:156-163`).

- **Pi symptom:** outbound channel messages succeed at the `send_chan_msg` level (the firmware just queues them) but never emit an actual `MSG_SENT`/`RX_LOG_DATA` echo. The library's `send_chan_msg` normally returns `MSG_SENT` from the firmware after enqueue, not after airtime, so this may still *appear* to succeed at the API level. CivicMesh's echo counter (`_on_rx_log_data` in `_setup_mesh_client`) never increments.
- **Radio symptom:** TX LED (GPIO35, via `onBeforeTransmit` in ESP32Board.h:88) stuck **ON**. OLED UI may show "transmitting" permanently.
- **Recovery:** Software warm-reset via SPI (the `sx126xResetAGC` helper at `SX126xReset.h:8-37` exists but is only invoked from `doResetAGC()` which is called by `resetAGC()` which is called… need to trace). If software can issue the warm-reset sequence, ESP32 reset followed by `radio_init()` recovers. If the SPI bus itself is wedged, only VEXT power-cycle recovers. VBUS cycle recovers.
- **Detection:** Stuck-on TX LED (only observable if visible to operator). No `MSG_SENT` ack within airtime budget. SX1262 `getIrqFlags()` could be polled but isn't by the current firmware.
- **Likelihood:** M. SX126x family has known errata around stuck TX states under certain SPI timing conditions; CivicMesh hits a lot of airtime.

### B2. SX1262 stuck in RX state

`startReceive()` succeeded, state is `STATE_RX`, but no `DIO1` interrupts are ever delivered even when packets arrive — either because `setPacketReceivedAction(setFlag)` (RadioLibWrappers.cpp:28) has been clobbered or because the SX1262 is not actually demodulating.

- **Pi symptom:** mesh_bot receives no `RX_LOG_DATA` events. `upsert_status(..., radio_connected=True)` still true. Appears "alive but deaf."
- **Radio symptom:** OLED normal. No TX LED activity unless something outbound is sent.
- **Recovery:** Same hierarchy as B1 — software warm reset if SPI still works, else ESP32 reset + re-init, else VEXT cycle.
- **Detection:** absence of `RX_LOG_DATA` for an unusually long period (hard to distinguish from a quiet mesh). `get_stats_core` still responds.
- **Likelihood:** M. Noise-floor sampling in `RadioLibWrapper::loop` (`RadioLibWrappers.cpp:76-94`) can drive `_noise_floor` low, locking sampling threshold in a way the code tries to defend against with `resetAGC` — but only when something external calls `triggerNoiseFloorCalibrate`.

### B3. SX1262 in undefined state after partial SPI transaction

ESP32 reset interrupts RadioLib in the middle of an SPI write (e.g., during `CALIBRATE 0x7F` or a register patch write). On the next boot, the SX1262 is in a half-configured state. `std_init` may succeed (the first `begin()` call tries to put the chip into standby before reconfiguring) or may fail with an SPI error code.

- **Pi symptom:** if `std_init` fails, firmware prints `ERROR: radio init failed: <status>` and halts. Pi sees that single line on the serial port, then nothing. All commands time out.
- **Radio symptom:** OLED "Loading…" forever. Radio silent.
- **Recovery:** VEXT power-cycle is the only thing that forces the SX1262 out of this state, because there is no hardware reset line (`P_LORA_RESET=RADIOLIB_NC`). ESP32 reset alone re-runs `std_init` which may succeed on retry, but if the chip is in a truly stuck state, only VEXT cycle works. VBUS cycle always recovers.
- **Detection:** `ERROR: radio init failed:` text on serial.
- **Likelihood:** L, but rises sharply in proportion to how often the Pi issues RTS resets mid-SPI.

### B4. SX1262 wedged by VEXT brownout

OLED / SX1262 power rail (VEXT, GPIO36) briefly collapses — from a bad connector, ripple, or a firmware that mis-manages `periph_power`. SX1262 enters an undefined state while ESP32 keeps running.

- **Pi symptom:** `get_stats_core` responds. Commands succeed but radio is deaf/mute.
- **Radio symptom:** OLED may glitch or be dark. TX LED may be wrong.
- **Recovery:** Firmware-initiated `periph_power.off(); delay; on()` in principle, but no code path currently does this recovery. ESP32 reset re-runs `radio_init`. VBUS cycle always.
- **Likelihood:** L.

## C — Serial framing

### C1. Serial framing desync, Pi → radio direction

Byte lost or corrupted on a Pi→radio frame. Firmware state machine in `ArduinoSerialInterface::checkRecvFrame` (`src/helpers/ArduinoSerialInterface.cpp:39-73`) is awaiting `_frame_len` bytes based on the corrupted length header. No timeout, no CRC. Subsequent valid frames are swallowed as payload until the accumulator aligns by coincidence.

- **Pi symptom:** outbound commands appear to send but produce no response, or sometimes produce wrong responses once alignment happens to repair.
- **Radio symptom:** normal (it's just silently mis-parsing).
- **Recovery:** The firmware does eventually reset state to `RECV_STATE_IDLE` after completing any frame, so alignment does repair when the accumulator exhausts and either (a) a subsequent `<` byte happens to land at the right place, or (b) the current garbled frame happens to end at an integer boundary. No software-initiated resync.
- **Detection:** command timeouts without any radio-side observable error.
- **Likelihood:** L on USB (reliable transport), higher at high CPU load on either end.

### C2. Serial framing desync, radio → Pi direction

Symmetric to C1. Library's frame parser (`serial_cx.py:handle_rx` at `serial_cx.py:76-123`) handles this better:

1. Searches for `0x3E` start byte (`serial_cx.py:79`), tolerant of leading junk.
2. Rejects frames claiming size > 300 bytes and resets parser state (`serial_cx.py:98-105`).

So the Pi-side parser is partially self-healing, but if a valid-looking size header (≤300) is parsed from corrupt data, the next payload bytes are absorbed as "frame content" — potentially swallowing a real subsequent frame.

- **Pi symptom:** missed events. For RX_LOG_DATA events, this means missed echo counts. If the swallowed frame was a command response, that command times out.
- **Radio symptom:** normal.
- **Recovery:** self-healing on next valid frame.
- **Detection:** intermittent missed responses. The reader logs `"Received data: <hex>"` at DEBUG (`reader.py:72`).
- **Likelihood:** L.

### C3. Concurrent serial port opens on the host

Two host processes hold open file descriptors on the same `/dev/ttyUSB0` simultaneously. Linux does not exclusive-lock USB-serial nodes by default, and pyserial opens without `TIOCEXCL`, so both `open()` calls succeed silently. Inbound bytes from the radio are split arbitrarily between the two readers (each sees fragments); outbound writes from the two processes interleave on the wire. The radio sees corrupt frames; the host-side parser hunts for `0x3E` start bytes in mid-frame garbage (`serial_cx.py:79`).

- **Pi symptom:** symptoms are nearly identical to **C1** and **C2** — command timeouts, missed `RX_LOG_DATA` events, occasional responses delivered to the wrong process. The `liveness_task` (`recovery.py`) polling `get_stats_core` times out because responses go to the other PID; this trips `RecoveryController` into RTS-reset thrash that cannot fix the underlying problem (file descriptors stay open across an ESP32 reset). Root cause is host-side, not the radio.
- **Radio symptom:** identical-looking to C1 from the radio's point of view — the ESP32 firmware sees malformed frames on its UART and silently mis-parses them. OLED and TX LED otherwise normal.
- **Recovery:** stop the duplicate process. Concrete command: `sudo systemctl stop civicmesh-mesh` (or `kill <PID>` for an offending dev process), then restart whichever side should remain. RTS pulse, ESP32 reset, and VBUS cycle do **not** help — they do not close the host file descriptors.
- **Detection:** **`lsof /dev/ttyUSB0`** and **`fuser /dev/ttyUSB0`** will both list multiple PIDs. That is the unambiguous signal. Without running those commands, the symptoms look identical to C1/C2 — an agent debugging "framing errors" should rule C3 out before chasing the radio. See also `docs/civicmesh-tool.md` (section "Running dev alongside prod") for the operator-side recipe.
- **Likelihood:** **L** — operator-induced, not hardware-driven. Most common trigger: a developer with prod installed at `/usr/local/civicmesh/` runs `uv run civicmesh-mesh` from their dev tree without first stopping the prod systemd service.

## D — USB / CP2102

### D1. CP2102 bridge hung while ESP32 fine

The CP2102 enters a state where it stops forwarding UART-to-USB traffic, even though the ESP32 is running. Possible causes: CP2102 firmware bug, ESD event, transient power glitch on USB D+/D−.

- **Pi symptom:** `connection_lost` may or may not fire. If the USB stack thinks the device is still present, pyserial has an open FD but no data ever arrives. All commands time out. `dmesg` may or may not log anything depending on whether the bridge fully dropped or just stopped forwarding.
- **Radio symptom:** ESP32 keeps running. OLED keeps updating. The ESP32 has no way to detect this — `ArduinoSerialInterface::isConnected()` always returns true (`src/helpers/ArduinoSerialInterface.cpp:16-18`). The firmware keeps `writeFrame`'ing to `Serial`; those bytes are buffered or dropped by the stalled CP2102.
- **Recovery:** RTS reset pulse does not reset the CP2102 (only ESP32). Physical RST button same. VBUS cycle (USB unplug) resets everything including CP2102. Host-side `ioctl(USBDEVFS_RESET)` on the `/dev/bus/usb/...` handle can renumerate the CP2102 without physical unplug *(inferred — the pyusb `reset()` method does this)*.
- **Detection:** no bytes arrive on the Pi after a deliberate command send. `lsusb` still shows the device ID. `dmesg -w` may be silent or may log "ttyUSB0: XYZ cts: timeout."
- **Likelihood:** **M** — CP2102 hangs are a documented class of bug, usually rare but not negligible under sustained load.

### D2. Host-side `ttyUSB` descriptor stale but device fine

The Pi's kernel has a stale `/dev/ttyUSB0` FD — typically happens after a disorderly close or if pyserial's async cleanup didn't complete. The device itself is responsive; any other process that opens the port gets fresh bytes.

- **Pi symptom:** within the current mesh_bot process, no data flows. `connection_lost` may not fire. From a parallel shell, `picocom /dev/ttyUSB0` sees normal traffic. `lsof /dev/ttyUSB0` shows mesh_bot holding it.
- **Radio symptom:** normal.
- **Recovery:** restart mesh_bot process. No hardware reset needed. VBUS cycle also works but is overkill.
- **Likelihood:** L.

### D3. `meshcore_py` holds port open but firmware stopped responding

Umbrella for any of A1/B1/B2/C1/D1 seen from the Pi. The `connection_lost` callback (`serial_cx.py:42-47`) only fires when pyserial's protocol reports the serial port itself closed — which happens on USB disconnect, not on radio-side hangs. So mesh_bot's `_connect_loop` has already returned (connect succeeded once, then `return`).

- **Pi symptom:** mesh_bot is "connected" per its heartbeat (`upsert_status` in `_heartbeat_task`), but all outbound commands time out and no inbound events arrive. **As of CIV-41 (2026-04-21), this scenario is detected and recovered automatically.** A `liveness_task` polls `get_stats_core` every 30s; 3 consecutive timeouts (≈90s) triggers recovery. An independent outbox trigger fires after 3 consecutive `send_chan_msg` failures. Both feed `RecoveryController` in `recovery.py`, which runs an RTS pulse to reset the ESP32, reconnects, and verifies via `get_stats_core` before declaring healthy. If the RTS reset doesn't clear the hang, the controller enters NEEDS_HUMAN and retries on exponential backoff (capped at 1 hour). See `docs/recovery.md`.
- **Recovery:** RTS pulse is the automated first step (resets ESP32; SX1262 may recover via `radio_init()` on reboot). Process restart or VBUS cycle (USB unplug, battery swap) remain the human-intervention fallbacks for hangs that RTS doesn't clear.
- **Detection:** `liveness_task` and outbox-failure trigger (see above). Status table `state` column reflects `recovering` or `needs_human`.
- **Likelihood:** H over multi-day deployment — it's the observable surface of every silent-hang failure.

## E — Storage / prefs

### E1. NVS / SPIFFS corruption at boot (auto-reformatted)

`SPIFFS.begin(true)` at main.cpp:186 mounts with `formatOnFail=true`. On corruption the filesystem is wiped without warning. Identity (`self_id`), contacts, channels, and prefs are all lost; the firmware generates a new identity in `MyMesh::begin` at `MyMesh.cpp:884-892`.

- **Pi symptom:** connect succeeds. `self_info.public_key` is different from before. CivicMesh currently does not check for identity change. `self_info.radio_*` returns the firmware defaults (LORA_FREQ/LORA_BW/LORA_SF/LORA_CR build flags) — these may or may not match `cfg.radio.*` so the `_radio_matches` check in `_setup_mesh_client` might trigger `set_radio` or might skip it.
- **Radio symptom:** normal — OLED shows new node name (first 4 hex bytes of new pubkey).
- **Recovery:** N/A — damage is done. Re-provisioning required to restore identity.
- **Detection:** self_info.public_key change, or the node's advert name changes, or nobody can reach it at its old pubkey.
- **Likelihood:** L but consequential. Worth the [test in § T6](test-plan.md).

### E2. Loss of prefs without format (partial corruption)

A pref file is unreadable but SPIFFS mount succeeds. `loadPrefs` returns defaults. Radio params may revert to build flags, which for CivicMesh means 869 MHz 250kHz SF11 CR5 (need to confirm in `LORA_*` defaults).

- **Pi symptom:** radio params mismatch → `_radio_matches` fails → `set_radio` is called. If `set_radio` succeeds, operation resumes. If it fails (happened on 1.11.0 — see comment in `_setup_mesh_client`), the session breaks.
- **Likelihood:** L.

## F — BLE / Wi-Fi stacks

### F1. BLE stack wedged

**Not applicable to companion_radio_usb.** The BLE stack is not compiled in for this build (verified: platformio.ini:139-156 shows no `BLE_PIN_CODE` and no `helpers/esp32/*.cpp` filter). In the `Heltec_v3_companion_radio_ble` build this would apply; omitted here.

### F2. Wi-Fi stack wedged

Same: not compiled in for USB build. Omitted.

## Summary table

| ID | Mode | Pi-side recoverable by restart? | Firmware self-recovers? | Needs RTS reset? | Needs USB unplug? | Likelihood |
|---|---|---|---|---|---|---|
| A1 | ESP32 hang | No | No | Yes | As fallback | M |
| A2 | A1 + auto-reset defeated | No | No | **No** | **Yes** | L/M |
| A3 | Panic → reboot | No action needed | Yes | N/A | N/A | M |
| A4 | Panic reboot loop | No | No | Does not help | Flash/wipe only | L |
| A5 | Task WDT | — | Yes | — | — | L |
| A6 | Int WDT | — | Yes | — | — | L |
| A7 | Brownout | — | Yes | — | — | L |
| B1 | SX1262 stuck TX | No | No (no code path) | Yes (via ESP32 reset) | Yes (VEXT) | M |
| B2 | SX1262 stuck RX | No | No (no code path) | Yes | Yes | M |
| B3 | SX1262 undefined after partial SPI | No | No | Maybe | **Yes (reliable)** | L |
| B4 | VEXT brownout | — | Sometimes | Yes | Yes | L |
| C1 | Pi→radio framing desync | Maybe | Eventually | Yes (safest) | Yes | L |
| C2 | Radio→Pi framing desync | Maybe | Yes | — | — | L |
| C3 | Concurrent serial opens (host-side) | Yes (kill duplicate) | N/A | Does not help | Does not help | L |
| D1 | CP2102 hung | No | N/A | **No** | **Yes** | M |
| D2 | Stale ttyUSB | Yes | N/A | — | Overkill | L |
| D3 | Silent hang, umbrella | No | Depends | Depends | Depends | H |
| E1 | SPIFFS reformat | No | Boots w/ new identity | N/A | N/A | L |
| E2 | Partial prefs loss | Sometimes | Sometimes | Yes | Yes | L |

"Needs USB unplug" = **Yes (reliable)** for modes where only a VBUS power cycle resets the specific failed component (CP2102 in D1, SX1262 in B3 when software paths fail).
