# Fix List

Known issues, mitigations, and deferred work items. Checked boxes are
resolved; unchecked boxes are open.

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
