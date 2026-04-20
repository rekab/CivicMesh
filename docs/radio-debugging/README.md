# Radio Debugging — Heltec V3 Companion

Failure-mode analysis for the Heltec V3 running MeshCore companion firmware, connected to a Raspberry Pi over USB-serial via the `meshcore_py` client. Read-only analysis. No recovery design here — that comes later, informed by empirical testing.

## Start here

- **Currently debugging a hang?** Read [`our-hang.md`](our-hang.md). It maps the specific "RTS reset didn't recover, RST button didn't recover, only USB unplug recovered" evidence to candidate failure modes.
- **Unfamiliar with how the radio boots or what "reset" actually does?** Read [`boot-and-reset.md`](boot-and-reset.md) first. The reset-domain table lives there and everything else links to it.
- **Planning bench tests?** [`test-plan.md`](test-plan.md) has one test per open question.

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
