#!/usr/bin/env bash
#
# check-port.sh — refuse to talk to anything that isn't the Inkplate.
#
# Wired into every Makefile target that opens the serial port. Guards
# against the dev/deploy Pi having both an Inkplate (CH340, VID 1a86)
# and a Heltec V3 (CP2102, VID 10c4, running MeshCore) plugged in at
# the same time. Flashing the Heltec port with an ESP32 Arduino sketch
# would brick the radio's MeshCore firmware including its keys.
#
# Usage: check-port.sh <serial-port>

set -euo pipefail

err()  { echo "check-port: ERROR: $*" >&2; }
note() { echo "check-port: $*" >&2; }

PORT="${1:-}"
if [ -z "$PORT" ]; then
    err "no port supplied"
    err "usage: $0 <serial-port>"
    err "hint: ls /dev/serial/by-id/"
    exit 1
fi

if [ ! -e "$PORT" ]; then
    err "port does not exist: $PORT"
    err "hint: plug the Inkplate in, then ls /dev/serial/by-id/"
    exit 1
fi

RESOLVED=$(readlink -f "$PORT")
if [ ! -e "$RESOLVED" ]; then
    err "port resolves to a non-existent device: $PORT -> $RESOLVED"
    exit 1
fi

# udevadm prints KEY=value lines; grab the USB VID specifically. Hard-fail
# if udevadm itself errors or the field is missing — never proceed on
# ambiguous information. The `|| true` keeps `set -e` from killing us
# before we can emit our own error message.
VID=$(udevadm info --name="$RESOLVED" --query=property 2>/dev/null \
        | awk -F= '$1 == "ID_VENDOR_ID" { print $2; exit }' || true)

if [ -z "$VID" ]; then
    err "could not read ID_VENDOR_ID for $RESOLVED via udevadm"
    err "is udev running? is the device a USB-serial adapter?"
    exit 1
fi

case "$VID" in
    10c4)
        err "$PORT (resolves to $RESOLVED) is a CP2102 device, VID=10c4"
        err "this is a Heltec / CP2102 device, REFUSING to flash to avoid bricking the radio"
        err "find the Inkplate (CH340, VID 1a86) with: ls /dev/serial/by-id/ | grep -i ch340"
        exit 1
        ;;
    1a86)
        note "OK: $PORT is CH340 (Inkplate)"
        exit 0
        ;;
    *)
        err "$PORT (resolves to $RESOLVED) has unexpected VID=$VID"
        err "expected 1a86 (CH340 / Inkplate); refusing to proceed"
        exit 1
        ;;
esac
