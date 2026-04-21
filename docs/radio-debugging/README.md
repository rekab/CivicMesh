# Radio Debugging — Heltec V3 Companion

Failure-mode analysis for the Heltec V3 running MeshCore companion firmware, connected to a Raspberry Pi over USB-serial via the `meshcore_py` client. Read-only analysis. No recovery design here — that comes later, informed by empirical testing.

## Start here

- **Currently debugging a hang?** Read [`our-hang.md`](our-hang.md). It maps the specific "RTS reset didn't recover, RST button didn't recover, only USB unplug recovered" evidence to candidate failure modes.
- **Unfamiliar with how the radio boots or what "reset" actually does?** Read [`boot-and-reset.md`](boot-and-reset.md) first. The reset-domain table lives there and everything else links to it.
- **Planning bench tests?** [`test-plan.md`](test-plan.md) has one test per open question.

## Structural findings

Five orthogonal facts from reading the firmware and library. These are the constraints that shaped the hypothesis ranking in `our-hang.md`; individual claims are cited in the linked docs.

1. **No watchdog anywhere in MeshCore.** Zero matches for `esp_task_wdt_*` across the firmware tree. The Arduino-ESP32 main task runs without a task WDT, so a pure firmware hang never self-recovers. See [`boot-and-reset.md §3`](boot-and-reset.md) and [`failure-modes.md A1`](failure-modes.md).
2. **SX1262 RESET pin is not wired on V3** (`P_LORA_RESET=RADIOLIB_NC` in `variants/heltec_v3/platformio.ini:13`). The only ways to reset the radio chip are a VEXT power-cycle (via `periph_power` on GPIO36) or a software warm-sleep SPI sequence. This is the structural reason "only USB unplug recovers" is a plausible signature. See [`boot-and-reset.md §2`](boot-and-reset.md#2-reset-domains) and [`failure-modes.md B-family`](failure-modes.md).
3. **Serial framing has no CRC, no escapes, no mid-frame timeout.** Pi→radio: `<` + 2-byte LE size + payload. Radio→Pi: `>` + 2-byte LE size + payload. The Pi-side parser is partially self-healing (searches for `0x3E`, rejects declared sizes > 300 bytes at `serial_cx.py:98-105`); the firmware side is not. See [`failure-modes.md C-family`](failure-modes.md).
4. **`ArduinoSerialInterface::isConnected()` returns true unconditionally** (`src/helpers/ArduinoSerialInterface.cpp:16-18`). Pi-side `connection_lost` only fires on physical USB disconnect, not on silent radio hang. CivicMesh's `_connect_loop` returns after a single successful connect with no silent-hang detection — matches the D3 umbrella mode in [`failure-modes.md`](failure-modes.md).
5. **1.11.0-specific `set_radio` note in CivicMesh** (`mesh_bot.py:320-321`) predates this analysis and independently supports hypothesis B3 (SX1262 stuck after an interrupted SPI transaction). See [`our-hang.md`](our-hang.md) surviving hypotheses.

## Files

| File | Purpose |
|---|---|
| [`boot-and-reset.md`](boot-and-reset.md) | Boot sequence from power-on to "ready"; reset-domain table; reset-reason decode |
| [`failure-modes.md`](failure-modes.md) | Enumerated failure modes with symptoms, recovery, detection, likelihood |
| [`our-hang.md`](our-hang.md) | Evidence analysis for the specific observed hang |
| [`test-plan.md`](test-plan.md) | Empirical test plan for Phase 2 |

## Pinned references

- **Firmware:** `~/code/meshcore-src/MeshCore` at `companion-v1.15.0` (detached HEAD).
- **Library:** `~/code/meshcore-src/meshcore_py` at `fbf84cb` (`main`; internal version `2.3.6`, no pushed tag).

Citation form in these documents: `path/inside/repo:line` for firmware/library, or `path:line` for CivicMesh paths relative to the repo root. Claims flagged "inferred" are reasoning from general ESP32 / SX126x / pyserial knowledge, not from reading code.

## Scope

This analysis targets the `Heltec_v3_companion_radio_usb` PlatformIO build (variants/heltec_v3/platformio.ini:139). That build uses `ArduinoSerialInterface` on default `Serial` (USB-CDC via the on-board CP2102), with no BLE and no WiFi stack compiled in, and the `ui-new` OLED UI. Other Heltec V3 build targets (`ble`, `wifi`, `repeater`, `room_server`) differ in relevant ways; they are mentioned only where contrast illuminates V3 companion behavior.
