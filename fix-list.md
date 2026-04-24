# Fix List

Known issues, mitigations, and deferred work items. Checked boxes are
resolved; unchecked boxes are open.

## Radio Reliability

- [x] **CIV-41: Silent-hang detection and software recovery** (resolved 2026-04-21, commits 89d7114, 9a5cdbc): mesh_bot had no way to detect or recover from a silent radio hang — the radio appeared connected but all commands timed out and no events arrived. Added `RecoveryController` in `recovery.py` with two detection triggers (liveness ping timeouts, outbox send failures) and an RTS-pulse recovery ladder. See `docs/recovery.md` and `docs/heltec-recovery.md`.

- [ ] **CIV-44: VEXT power-cycle firmware command**: The RTS pulse (step 1) resets the ESP32 but cannot reset the SX1262 radio chip. A stuck SX1262 requires a VEXT power cycle, which needs a new `CMD_POWER_CYCLE_RADIO` serial command in the MeshCore companion firmware. Deferred to post-Toorcamp. See `docs/heltec-recovery.md` § "VEXT power cycle".

- [ ] **CIV-30: GPIO EN toggle hardware mod**: Direct GPIO wire from Pi to Heltec CHIP_PU (EN) pin, bypassing the CP2102. Adds a recovery path when the USB-serial bridge is hung. Not yet wired.

## Hardware & Deployment

- [ ] **USB serial port can rename after re-enumeration**: `/dev/ttyUSB0` becomes `/dev/ttyUSB1` (or higher) after any USB re-enumeration event — pyusb `device.reset()`, cable wiggle, CP2102 glitch. Config using `/dev/ttyUSB0` will fail to reconnect until physical unplug.

  **Mitigation (done in config.toml):** use `/dev/serial/by-id/usb-Silicon_Labs_CP2102_*` symlink, which is stable across re-enumeration.

  **Caveat — ambiguous serial number:** Heltec V3 CP2102 chips appear to ship with the Silicon Labs factory-default serial `0001`, not a unique per-device serial. Every Heltec V3 on this hardware batch will have the same `by-id` path. This means:
  - If you ever have two Heltec V3s on the same Pi (future expansion, bench testing, etc.), the `by-id` symlink will be ambiguous and one of them will be inaccessible by path.
  - Mitigations if/when this becomes relevant:
    1. Write unique serials to each CP2102 using Silicon Labs' `cp210x-cfg` tool (one-time per device).
    2. Or use a udev rule matching by bus topology (`KERNELS==...`) to create a stable name per physical USB port.
    3. Or accept "one Heltec per Pi" as a constraint (current deployment model).
  - Not a blocker for current single-device deployments; flag for post-Toorcamp if multi-radio deployments become interesting.

  **Where this was found:** while testing `diagnostics/radio/recovery_characterization.py` — pyusb `device.reset()` worked electrically (device re-enumerated in <1s per dmesg) but the script timed out waiting for `/dev/ttyUSB0` to reappear, because the kernel had attached it at `/dev/ttyUSB1`.

- [ ] **pyusb requires udev rule for non-root access**: By default, only root can send USB control transfers (like `device.reset()`) to the CP2102. Without a udev rule, pyusb raises `USBError: [Errno 13] Access denied`. The recovery characterization script and any future lifecycle manager using pyusb resets need this rule in place.

  **Mitigation (one-time per Pi):**
  ```bash
  sudo tee /etc/udev/rules.d/99-cp2102.rules > /dev/null <<'EOF'
  SUBSYSTEM=="usb", ATTR{idVendor}=="10c4", MODE="0666"
  EOF
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  ```

  This grants all users read/write access to Silicon Labs (VID `10c4`) USB devices. Applies immediately to newly attached devices; `udevadm trigger` re-applies to already-attached devices without requiring unplug.
