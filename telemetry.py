import asyncio
import functools
import json
import os
import subprocess
import time
from typing import Optional

from database import (
    DBConfig,
    get_outbox_snapshot,
    insert_telemetry_event,
    insert_telemetry_sample,
)


# ---------------------------------------------------------------------------
# Throttle bit definitions
# ---------------------------------------------------------------------------

_THROTTLE_BITS = {
    0: "Under-voltage detected",
    1: "Arm frequency capped",
    2: "Currently throttled",
    3: "Soft temperature limit active",
    16: "Under-voltage has occurred",
    17: "Arm frequency capping has occurred",
    18: "Throttling has occurred",
    19: "Soft temperature limit has occurred",
}

# Module-level state for throttle change detection.
# None means "no prior reading yet" — skip emission on first sample.
_last_throttled_bitmask: Optional[int] = None


# ---------------------------------------------------------------------------
# Reader functions — each returns None on missing/unsupported hardware
# ---------------------------------------------------------------------------

def read_uptime_sec() -> Optional[int]:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def read_loadavg() -> Optional[tuple[float, float, float]]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            return float(parts[0]), float(parts[1]), float(parts[2])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def read_cpu_temp_c() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (FileNotFoundError, OSError, ValueError):
        return None


def read_meminfo() -> Optional[dict[str, int]]:
    try:
        result = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    result["MemTotal"] = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    result["MemAvailable"] = int(line.split()[1])
                if len(result) == 2:
                    break
        return result if len(result) == 2 else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def read_disk_free(path: str = "/") -> Optional[tuple[int, int]]:
    try:
        st = os.statvfs(path)
        free_kb = (st.f_bavail * st.f_frsize) // 1024
        total_kb = (st.f_blocks * st.f_frsize) // 1024
        return free_kb, total_kb
    except (OSError, AttributeError):
        return None


def read_net_bytes() -> Optional[tuple[int, int]]:
    try:
        rx_total, tx_total = 0, 0
        with open("/proc/net/dev") as f:
            for line in f:
                line = line.strip()
                if ":" not in line:
                    continue
                iface, data = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                fields = data.split()
                rx_total += int(fields[0])
                tx_total += int(fields[8])
        return rx_total, tx_total
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def read_throttled() -> Optional[int]:
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        # Output is like "throttled=0x0"
        val = result.stdout.strip().split("=")[-1]
        return int(val, 16)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Throttle change detection
# ---------------------------------------------------------------------------

def _decode_throttle_bits(bitmask: int) -> list[str]:
    return [label for bit, label in sorted(_THROTTLE_BITS.items()) if bitmask & (1 << bit)]


def _check_throttle_change(db_cfg: DBConfig, ts: int, new_bitmask: Optional[int], log=None) -> None:
    global _last_throttled_bitmask

    if new_bitmask is None:
        return
    if _last_throttled_bitmask is None:
        _last_throttled_bitmask = new_bitmask
        return
    if new_bitmask == _last_throttled_bitmask:
        return

    old = _last_throttled_bitmask
    _last_throttled_bitmask = new_bitmask

    xor = old ^ new_bitmask
    changed = []
    for bit, label in sorted(_THROTTLE_BITS.items()):
        if xor & (1 << bit):
            if new_bitmask & (1 << bit):
                changed.append(f"{label} (+)")
            else:
                changed.append(f"{label} (-)")

    detail = {
        "old": f"0x{old:x}",
        "new": f"0x{new_bitmask:x}",
        "changed_bits": changed,
        "active_now": _decode_throttle_bits(new_bitmask),
    }
    insert_telemetry_event(db_cfg, ts=ts, kind="throttle_change", detail=detail, log=log)
    if log:
        log.info("telemetry:throttle_change old=0x%x new=0x%x changed=%s", old, new_bitmask, changed)


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------

def sample_once(db_cfg: DBConfig, log=None, sample_throttled: bool = False) -> None:
    ts = int(time.time())

    uptime = read_uptime_sec()
    loadavg = read_loadavg()
    cpu_temp = read_cpu_temp_c()
    meminfo = read_meminfo()
    disk = read_disk_free()
    net = read_net_bytes()

    throttled = read_throttled() if sample_throttled else None

    depth, oldest_age_s = get_outbox_snapshot(db_cfg, now_ts=ts, log=log)

    insert_telemetry_sample(
        db_cfg,
        ts=ts,
        uptime_s=uptime,
        load_1m=loadavg[0] if loadavg else None,
        cpu_temp_c=cpu_temp,
        mem_available_kb=meminfo["MemAvailable"] if meminfo else None,
        mem_total_kb=meminfo["MemTotal"] if meminfo else None,
        disk_free_kb=disk[0] if disk else None,
        disk_total_kb=disk[1] if disk else None,
        net_rx_bytes=net[0] if net else None,
        net_tx_bytes=net[1] if net else None,
        outbox_depth=depth,
        outbox_oldest_age_s=oldest_age_s,
        throttled_bitmask=throttled,
        log=log,
    )

    if sample_throttled:
        _check_throttle_change(db_cfg, ts, throttled, log=log)


# ---------------------------------------------------------------------------
# Async loop (runs inside mesh_bot's event loop)
# ---------------------------------------------------------------------------

async def telemetry_loop(db_cfg: DBConfig, log) -> None:
    tick = 0
    while True:
        try:
            do_throttle = (tick % 5 == 0)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                functools.partial(sample_once, db_cfg, log=log, sample_throttled=do_throttle),
            )
        except Exception:
            if log:
                log.exception("telemetry:sample_failed")
        tick += 1
        await asyncio.sleep(60)
