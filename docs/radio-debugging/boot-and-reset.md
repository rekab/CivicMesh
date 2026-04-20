# Boot and Reset

Pinned refs: MeshCore `companion-v1.15.0`, meshcore_py `fbf84cb`. Build target: `env:Heltec_v3_companion_radio_usb` (`variants/heltec_v3/platformio.ini:139`).

## 1. Boot sequence

### 1.1 ROM bootloader → 2nd-stage bootloader → app

The ESP32-S3 starts at the internal ROM bootloader. On release of `CHIP_PU` (the chip-enable pin wired to the auto-reset transistor) the ROM reads strapping pins — principally `GPIO0`, `GPIO3`, `GPIO45`, `GPIO46` — to decide between SPI flash boot, USB download mode, and JTAG. In normal operation `GPIO0 = 1` at boot, so the ROM loads the 2nd-stage bootloader from flash and prints the familiar banner:

```
ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x1 (POWERON),boot:0x29 (SPI_FAST_FLASH_BOOT)
```

That banner is emitted by ROM code before any user firmware runs. It is the earliest signal available on the UART and the only direct evidence of the raw reset reason from the chip's perspective (see reset-reason decode, §3). *(Inferred from ESP32-S3 TRM / Espressif boot docs — the ROM code itself is not in the MeshCore repo.)*

The 2nd-stage bootloader then loads the Arduino-ESP32 / ESP-IDF app binary from the `app0` partition, initializes the heap, sets up the Arduino main task, and calls the user `setup()` function.

### 1.2 Arduino `setup()`

Source: `examples/companion_radio/main.cpp:108-223`.

Ordered steps (companion_radio_usb build):

1. **`Serial.begin(115200)`** (main.cpp:109). Opens the USB-CDC-over-CP2102 logical serial port. On the V3 the CP2102 bridges UART0 on the ESP32 to the host USB. *(Inferred: no separate USB-CDC-direct configuration present in the v3 build flags.)*
2. **`board.begin()`** (main.cpp:111) → `HeltecV3Board::begin()` (`variants/heltec_v3/HeltecV3Board.h:31-53`). In order:
   - `ESP32Board::begin()` (`src/helpers/ESP32Board.h:23-49`): sets `startup_reason = BD_STARTUP_NORMAL`, calls `setCpuFrequencyMhz(80)` (from `ESP32_CPU_FREQ=80` build flag), configures ADC on `PIN_VBAT_READ=1` and `P_LORA_TX_LED=35`, starts `Wire` on I²C pins `SDA=17 / SCL=18`.
   - Auto-detects ADC-control pin polarity and initializes it inactive.
   - `periph_power.begin()` on `PIN_VEXT_EN=36` — this gates VEXT, the 3.3V rail that powers the OLED, the SX1262, and (on V3) the TCXO. Initial state after `begin()` is determined by `RefCountedDigitalPin`: uses a refcount, starts at zero, so VEXT is driven active only when something `acquire()`s it. *(To confirm: read RefCountedDigitalPin.h.)* The fact that the radio works at boot implies either the SX1262 wrapper or some init step `acquire()`s it before `radio.std_init()` runs.
   - Reads `esp_reset_reason()` and special-cases `ESP_RST_DEEPSLEEP`: if the deep-sleep wakeup source was `ext1` on `P_LORA_DIO_1`, sets `startup_reason = BD_STARTUP_RX_PACKET`; clears RTC-GPIO holds on `P_LORA_NSS` and deinits the `P_LORA_DIO_1` RTC-GPIO configuration. For any other reset reason this branch is skipped — the startup_reason remains `BD_STARTUP_NORMAL`.
3. **`display.begin()`** (main.cpp:115) → `SSD1306Display` init over I²C. Failure returns false and is handled gracefully — the chip continues without a display. On success writes "Loading…" and ends the frame.
4. **`radio_init()`** (main.cpp:126) → `variants/heltec_v3/target.cpp:31-40` → `CustomSX1262::std_init(&spi)` (`src/helpers/radiolib/CustomSX1262.h:15-87`):
   - `rtc_clock.begin(Wire)` — `AutoDiscoverRTCClock` falls back to `ESP32RTCClock` on V3 (no external RTC). `ESP32RTCClock::begin()` (`src/helpers/ESP32Board.h:138-147`) sets the time to `15 May 2024, 8:50pm` **only if `esp_reset_reason() == ESP_RST_POWERON`**. This is relevant: after a clean RTS reset that produces `ESP_RST_POWERON`, the clock is wound back. After a `esp_restart()` or watchdog reset that produces a different reason, the clock keeps running from its RTC value.
   - `spi.begin(P_LORA_SCLK=9, P_LORA_MISO=11, P_LORA_MOSI=10)`.
   - `SX1262::begin(LORA_FREQ, LORA_BW, LORA_SF, cr=5, RADIOLIB_SX126X_SYNC_WORD_PRIVATE, LORA_TX_POWER=22, 16, tcxo=1.8)`.
   - On `-707/-706` SPI command errors, retries with `tcxo=0.0f` (CustomSX1262.h:47-50).
   - If init still fails: prints `"ERROR: radio init failed: <status>"` to `Serial` and returns false. Caller in main.cpp:126 then calls `halt()` — a bare `while(1);` (main.cpp:104-106).
   - The SX1262 RESET line is not wired to an ESP32 GPIO on V3: `P_LORA_RESET=RADIOLIB_NC` (platformio.ini:13 inside the Heltec V3 variant file). RadioLib's init sequence therefore cannot drive a hardware reset before SPI init; it relies on the chip being in a post-reset state already, or being brought there by a warm-sleep SPI sequence. **Consequence:** a wedged SX1262 cannot be hardware-reset by firmware short of power-cycling VEXT.
5. **`fast_rng.begin(radio_get_rng_seed())`** (main.cpp:128) — seeds PRNG from SX1262 noise.
6. **`SPIFFS.begin(true)`** (main.cpp:186) — mounts SPIFFS with `formatOnFail=true`. If the filesystem is corrupt or un-mounted, SPIFFS will silently reformat. Node identity, contacts, and prefs are stored here via `DataStore` (examples/companion_radio/main.cpp:34, `DataStore(SPIFFS, rtc_clock)`). A reformat is a silent loss of identity and prefs.
7. **`store.begin()`** (main.cpp:187) — initializes `DataStore`.
8. **`the_mesh.begin(disp != NULL)`** (main.cpp:188-194) → `MyMesh::begin()` (`examples/companion_radio/MyMesh.cpp:881-959`):
   - Load or generate main identity (`_store->loadMainIdentity` then random-generate if absent).
   - Set default node name (ADVERT_NAME build flag or first-4-bytes-of-pubkey hex).
   - Load persisted prefs via `_store->loadPrefs`; clamp them to sane ranges (lines 919-927).
   - Resolve BLE PIN (companion_radio_usb has no `BLE_PIN_CODE`, so `_active_ble_pin = 0`).
   - `resetContacts()`, `_store->loadContacts()`, `bootstrapRTCfromContacts()`, `addChannel("Public", PUBLIC_GROUP_PSK)`, `_store->loadChannels()`.
   - Apply persisted radio params: `radio_set_params(freq, bw, sf, cr)` + `radio_set_tx_power(tx_power_dbm)` + `setRxBoostedGainMode(rx_boosted_gain)`.
9. **`serial_interface.begin(Serial)`** (main.cpp:207) — `ArduinoSerialInterface` stores the `Stream*` pointer.
10. **`the_mesh.startInterface(serial_interface)`** (main.cpp:209) → `MyMesh::startInterface` (MyMesh.cpp:989-992) → `serial.enable()` — sets `_isEnabled = true` and `_state = RECV_STATE_IDLE` (`src/helpers/ArduinoSerialInterface.cpp:8-11`). **This is the "ready for serial commands" point.**
11. `sensors.begin()` (main.cpp:214), `ui_task.begin(...)` (main.cpp:221) — no sensors on a plain V3, so sensors is a no-op subclass. UITask sets up the OLED refresh loop.

### 1.3 Steady state

`loop()` (main.cpp:225-232) does three things every iteration, in order:

1. `the_mesh.loop()` → `MyMesh::loop()` (MyMesh.cpp:2160-2178): calls `BaseChatMesh::loop()` (which drives the radio state machine, routing, retransmit, etc.), then `checkSerialInterface()` which polls `_serial->checkRecvFrame(cmd_frame)` and dispatches commands.
2. `sensors.loop()`.
3. `ui_task.loop()` + `rtc_clock.tick()`.

There is no explicit task watchdog feed anywhere in MeshCore. The Arduino-ESP32 framework disables the task WDT on the main loop task by default. *(Inferred from absence of any `esp_task_wdt_add`/`esp_task_wdt_reset` calls in the MeshCore source tree — grep confirms zero matches.)*

### 1.4 What can fail silently vs loudly in setup

| Step | Silent failure? | Loud failure? |
|---|---|---|
| `Serial.begin` | No failure path — USB enumeration is out-of-band | Host-side `/dev/ttyUSB*` just doesn't appear |
| `board.begin` | I²C bus hung, ADC polarity misdetected (OLED may not work, but chip continues) | None |
| `display.begin` | Wrong I²C addr or hung bus returns false, execution continues without display | None |
| **`radio_init` failure** | **No — halts the chip via `while(1);`.** UART prints error then chip spins forever with no WDT, `loop()` never reached | Serial banner + error line visible to host |
| `SPIFFS.begin(true)` on corrupt FS | **Yes — silent reformat, identity & prefs lost** | Possibly a MESH_DEBUG line if enabled; MESH_DEBUG is explicitly disabled for this build (`// NOTE: DO NOT ENABLE -->  -D MESH_DEBUG=1`, platformio.ini:148) |
| `store.begin` / `loadMainIdentity` | If identity is missing or unreadable, generates a new one with no warning to host — old contacts would still load but the node's public key changes | None |
| `serial_interface.begin` | No failure path — just stores the pointer | None |

## 2. Reset domains

Each row: what the mechanism asserts electrically, what it resets, what it does NOT reset, and the `RTC_CNTL_RESET_REASON` code it produces.

| Mechanism | Electrical effect | Resets | Does NOT reset | Reset-reason code |
|---|---|---|---|---|
| **VBUS power cycle** (unplug USB) | VBUS falls, onboard 3.3V LDO drops, chip loses power entirely | Everything on-board: ESP32-S3 (RAM + RTC), SX1262, CP2102, OLED, NVS state in flash is preserved. RTC domain is fully lost because the V3 has no RTC battery. | Flash contents (prefs, identity, SPIFFS) — persisted across power loss | On next power-up: `ESP_RST_POWERON` (0x1) |
| **Auto-reset transistor via RTS pulse**<br>(what CivicMesh's test code does: `dtr=False; rts=True; sleep 0.1; rts=False`) | Pulls `CHIP_PU` (EN) low via the standard Espressif two-transistor circuit while IO0 stays high. *(Inferred: the V3 schematic is not in this repo, but the fact that this pattern produces `rst:0x1 (POWERON)` is consistent only with `CHIP_PU` being asserted; any other outcome would rule out a standard circuit.)* | ESP32-S3 digital core + RTC domain as if powered off. Does NOT reset SX1262 (no connection between ESP32 EN and SX1262 RESET on V3), does NOT reset CP2102 (separate bridge IC on its own power), does NOT reset OLED (powered by VEXT, which is driven by GPIO36 — held by ESP32 state before reset, then floats during reset then is re-driven after boot). | SX1262 internal state, CP2102 state, OLED contents (though VEXT glitch may reset OLED), ttyUSB host-side descriptor | `ESP_RST_POWERON` (0x1) — confirmed in the observed `rst:0x1 (POWERON)` banner |
| **Physical RST button** on V3 | Shorts `CHIP_PU` to GND directly via the push-button | Same as RTS auto-reset: ESP32-S3 core + RTC domain. Does NOT touch SX1262, CP2102, or VEXT ring (except by the same EN-floats side-effect). | SX1262, CP2102, OLED persistence | `ESP_RST_POWERON` (0x1) *(inferred: the electrical effect is identical to the transistor path, so the reset cause register sees the same condition)* |
| **`esp_restart()` from firmware** (`src/helpers/ESP32Board.h:125`, called by the CLI `reboot` command at MyMesh.cpp:2124) | Software-initiated system reset via RTC_CNTL. Does NOT toggle `CHIP_PU`. | ESP32-S3 digital core and peripherals. RTC domain is preserved (RTC memory, RTC timer keep running). | SX1262, CP2102, OLED | `ESP_RST_SW` (0x3) |
| **Task watchdog timeout** | ESP-IDF panics, logs a backtrace over UART, then issues a reset equivalent to `esp_restart()` | ESP32 core | SX1262, CP2102 | `ESP_RST_TASK_WDT` (0x7) — *but note: no code in MeshCore enables the task WDT, so this should not fire under normal operation* |
| **Interrupt watchdog timeout** | ESP-IDF panics; resets the core | ESP32 core | SX1262, CP2102 | `ESP_RST_INT_WDT` (0x6) — the IWDT is ESP-IDF default and fires if an ISR holds off scheduling for >300ms (default config). Rare but possible. |
| **Brownout detector** | Hardware comparator trips when VDD falls below threshold (default ~2.44V on S3) | ESP32 core | SX1262 (unless VEXT also collapses), CP2102 | `ESP_RST_BROWNOUT` (0xF) |
| **Panic / unhandled exception** | ESP-IDF panic handler runs, logs backtrace, then resets | ESP32 core | SX1262, CP2102 | `ESP_RST_PANIC` (0xA) |
| **Deep sleep wakeup** | `esp_deep_sleep_start()` called (e.g. from `HeltecV3Board::enterDeepSleep`). Not used by companion_usb in this firmware (no code path invokes it). | RTC wake resets most of the chip; specific GPIOs can be held | Only mentioned for completeness — companion_usb doesn't enter deep sleep. | `ESP_RST_DEEPSLEEP` (0x5) |
| **SX1262 RESET pin** | **Not wired on V3** (`P_LORA_RESET=RADIOLIB_NC`). The radio cannot be hardware-reset from the ESP32. | — | — | N/A — no register; the radio simply re-enters its default state after VEXT cycle |
| **SX1262 VEXT power-cycle** | Firmware drops then re-raises GPIO36 (`PIN_VEXT_EN`) via `periph_power`. Cuts the 3.3V rail feeding the SX1262, OLED, TCXO. | SX1262 to power-on defaults, OLED, TCXO | ESP32 core, CP2102, NVS | From the ESP32's POV this is not a system reset; `esp_reset_reason()` is unchanged |
| **SX1262 software "warm reset"** (warm sleep + standby + calibrate, see `src/helpers/radiolib/SX126xReset.h:8-37`) | Puts the SX1262 into warm sleep (retains config), then standby-RC, then issues a `CALIBRATE 0x7F` SPI command | Analog frontend / AGC / image calibration | SX1262 digital register state, ESP32 | N/A — radio-internal only |
| **CP2102 reset** | **No dedicated reset line exposed on V3** *(inferred: Heltec V3 schematic not in repo; standard CP2102N boards wire reset only to VBUS power and an optional RESET pad rarely brought out. The observed USB-unplug-recovers behavior is consistent with this).* A host-side `ioctl(USBDEVFS_RESET)` on `/dev/bus/usb/...` will force the CP2102 to renumerate without physical unplug. | CP2102 internal state (line levels, renumerates to host) | ESP32, SX1262 | N/A |
| **Host-side ttyUSB descriptor stale** | Not a device reset — just a stale kernel state on the Pi. `close(2)` + `open(2)` on `/dev/ttyUSB0` typically clears it; in extreme cases `echo 0 > /sys/bus/usb/devices/.../authorized; echo 1 > .../authorized` | Nothing on the radio | Everything on the radio | N/A |

## 3. Reset-reason decode (ESP32-S3)

Produced by `esp_reset_reason()` (`esp_system/include/esp_system.h`, ESP-IDF). The ROM bootloader's `rst:0x<N>` banner uses the raw `RTC_CNTL_RESET_REASON` value which maps to the enum below. Mapping is from the ESP32-S3 TRM §9 (reset and clock) and `components/esp_hw_support/port/esp32s3/rtc_time.c` in ESP-IDF — both external to this repo.

| Enum | Raw code | Meaning | Produced by |
|---|---|---|---|
| `ESP_RST_UNKNOWN` | 0 | Cannot be determined | *(rare; typically only on first boot after flash)* |
| `ESP_RST_POWERON` | 1 (`rst:0x1`) | Power-on (VBUS or `CHIP_PU` asserted) | VBUS cycle, RTS auto-reset pulse, RST button, host-side USB power-cycle |
| `ESP_RST_EXT` | 2 | External pin reset | Not applicable on S3 — no separate ext-reset pin (EN is the only reset input) |
| `ESP_RST_SW` | 3 | Software reset via `esp_restart()` / system-level API | MyMesh CLI "reboot" command; any firmware code path that calls `board.reboot()` |
| `ESP_RST_PANIC` | 4 | Exception / panic | Unhandled exception, stack overflow, assert failure |
| `ESP_RST_INT_WDT` | 5 | Interrupt watchdog | ISR disabled interrupts for >300ms (ESP-IDF default) |
| `ESP_RST_TASK_WDT` | 6 | Task watchdog | Subscribed task failed to feed WDT within timeout. *Not armed for the Arduino main task on this firmware.* |
| `ESP_RST_WDT` | 7 | Other/unspecified watchdog | RTC WDT |
| `ESP_RST_DEEPSLEEP` | 8 | Wakeup from deep sleep | `esp_deep_sleep_start()` + wake source |
| `ESP_RST_BROWNOUT` | 9 | Brownout reset (hardware) | VDD dropped below threshold |
| `ESP_RST_SDIO` | 10 | SDIO reset | N/A on V3 |
| `ESP_RST_USB` | 11 | USB-peripheral-triggered reset | N/A on V3 (USB goes via external CP2102, not the S3 native USB) |
| `ESP_RST_JTAG` | 12 | JTAG reset | Only if debugger is connected |
| `ESP_RST_EFUSE` | 13 | eFuse error | Hardware fault |
| `ESP_RST_PWR_GLITCH` | 14 | Power glitch detected | Hardware fault |
| `ESP_RST_CPU_LOCKUP` | 15 | CPU lockup detected | Hardware self-check |

*(Some codes: the exact numeric values above come from the ESP-IDF `esp_reset_reason_t` enum for S3; the raw ROM-banner `rst:0x<N>` values may be from `RTC_CNTL_RESET_REASON` register which uses a different encoding. Double-check numeric values against the target IDF version before using them for telemetry; I have not verified them line-for-line against the PlatformIO-bundled Arduino-ESP32 version used by this build.)*

## Open questions referenced by other files

- **Q1: Can RTS reset be issued *while the ESP32 is hung*?** If the hang somehow holds the GPIOs controlling the auto-reset transistor in a state that prevents `CHIP_PU` from actually falling, the toggle is ignored. See [failure-modes.md § A2](failure-modes.md) and [test-plan.md § T2](test-plan.md).
- **Q2: Does the physical RST button route through any logic the ESP32 could hold?** Standard practice is that the button shorts `CHIP_PU` to GND directly (no MCU involvement), but I have not verified the V3 schematic. See [test-plan.md § T1](test-plan.md).
- **Q3: If SPIFFS `formatOnFail=true` triggers, what is the Pi-side symptom?** Probably a longer-than-normal boot but successful reconnect; unclear whether the new identity would be noticeable to mesh_bot. See [failure-modes.md § E1](failure-modes.md).
- **Q4: Does `MESH_DEBUG` being disabled for this build suppress the SX1262 reset-agc `MESH_DEBUG_PRINTLN` in RadioLibWrappers.cpp:92 etc.?** If yes (which is the purpose of disabling it), we lose all in-band diagnostics for radio state transitions. See [test-plan.md § T8](test-plan.md).
