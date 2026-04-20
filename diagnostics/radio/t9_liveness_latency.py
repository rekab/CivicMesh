"""T9 -- Liveness latency characterization.

Measures get_stats_core() round-trip time at configurable poll intervals
to characterize baseline latency for a future liveness-ping feature.

USB-serial access is exclusive -- mesh_bot must be stopped before running.
"Realistic traffic" means whatever RX the radio processes on its own
(adverts, routing, channel messages from other nodes) while the Pi does
no TX.

Usage:
    python -m diagnostics.radio.t9_liveness_latency \\
        --config config.toml \\
        --intervals 10,30,60 \\
        --duration-per-interval 1200 \\
        --command-timeout 2.0 \\
        --out diagnostics/radio/runs/t9_run_$(date +%%Y%%m%%d_%%H%%M%%S).jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from meshcore import MeshCore  # noqa: E402

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(fh, kind: str, **fields) -> None:
    """Write one JSONL record.  Every record has 'kind' and 'ts'."""
    record = {"kind": kind, "ts": int(time.time()), **fields}
    fh.write(json.dumps(record, default=str) + "\n")
    fh.flush()


def _snapshot_self_info(mesh_client) -> dict:
    info = getattr(mesh_client, "self_info", {}) or {}
    return dict(info)


def _check_radio_params(self_info: dict, cfg_radio) -> list[str]:
    """Return list of mismatch warnings (empty if all match)."""
    checks = {
        "radio_freq": cfg_radio.freq_mhz,
        "radio_bw": cfg_radio.bw_khz,
        "radio_sf": cfg_radio.sf,
        "radio_cr": cfg_radio.cr,
    }
    warnings = []
    for key, want in checks.items():
        got = self_info.get(key)
        if got is None or float(got) != float(want):
            warnings.append(f"{key}: config={want} radio={got}")
    return warnings


def _cpu_percent_snapshot() -> float | None:
    if _HAS_PSUTIL:
        return psutil.cpu_percent(interval=None)
    return None


def _percentiles(values: list[float], pcts: list[float]) -> dict[float, float | None]:
    """Linear-interpolation percentiles (copied from verdict.py)."""
    if not values:
        return {p: None for p in pcts}
    sorted_vals = sorted(values)
    out: dict[float, float | None] = {}
    for p in pcts:
        if p <= 0:
            out[p] = sorted_vals[0]
            continue
        if p >= 100:
            out[p] = sorted_vals[-1]
            continue
        k = (len(sorted_vals) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(sorted_vals) - 1)
        if f == c:
            out[p] = sorted_vals[f]
        else:
            out[p] = sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)
    return out


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------

def _check_preconditions(serial_port: str) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "mesh_bot"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() == "active":
            print(
                "FATAL: mesh_bot.service is active.  Stop it first:\n"
                "  sudo systemctl stop mesh_bot",
                file=sys.stderr,
            )
            sys.exit(1)
    except FileNotFoundError:
        pass  # systemctl not available (dev machine)
    except subprocess.TimeoutExpired:
        print("WARNING: systemctl timed out; skipping mesh_bot check", file=sys.stderr)

    if not os.path.exists(serial_port):
        print(f"FATAL: serial port {serial_port} does not exist", file=sys.stderr)
        sys.exit(1)
    if not os.access(serial_port, os.R_OK | os.W_OK):
        print(f"FATAL: serial port {serial_port} is not readable/writable", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def _run_interval(
    mesh_client,
    interval_s: int,
    duration_s: int,
    command_timeout: float,
    fh,
    shutdown_event: asyncio.Event,
) -> None:
    load_before = os.getloadavg()
    cpu_before = _cpu_percent_snapshot()

    deadline = time.monotonic() + duration_s
    consecutive_timeouts = 0
    ok_latencies: list[float] = []
    n_ok = 0
    n_timeout = 0
    n_error = 0

    while time.monotonic() < deadline and not shutdown_event.is_set():
        t0 = time.monotonic()
        result_type = None
        error_reason = None
        payload_keys: list[str] = []

        try:
            result = await asyncio.wait_for(
                mesh_client.commands.get_stats_core(),
                timeout=command_timeout,
            )
            latency_ms = (time.monotonic() - t0) * 1000.0
            result_type = str(result.type) if hasattr(result, "type") else "ok"
            if hasattr(result, "payload") and isinstance(result.payload, dict):
                payload_keys = list(result.payload.keys())
            n_ok += 1
            consecutive_timeouts = 0
            ok_latencies.append(latency_ms)
        except asyncio.TimeoutError:
            latency_ms = (time.monotonic() - t0) * 1000.0
            result_type = "timeout"
            error_reason = "timeout"
            n_timeout += 1
            consecutive_timeouts += 1
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            result_type = "error"
            error_reason = f"{type(exc).__name__}: {exc}"
            n_error += 1
            consecutive_timeouts = 0

        _emit(
            fh, "sample",
            interval_s=interval_s,
            latency_ms=round(latency_ms, 2),
            result_type=result_type,
            error_reason=error_reason,
            payload_keys=payload_keys,
        )

        if consecutive_timeouts >= 3 and consecutive_timeouts % 3 == 0:
            _emit(
                fh, "event",
                event="silence_detected",
                consecutive_timeouts=consecutive_timeouts,
            )
            print(
                f"  silence_detected: {consecutive_timeouts} consecutive timeouts",
                file=sys.stderr,
            )

        # Sleep remainder of interval; wake instantly on shutdown signal
        elapsed = time.monotonic() - t0
        sleep_s = max(0, interval_s - elapsed)
        if sleep_s > 0:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_s)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal — sleep expired

    # Summary
    load_after = os.getloadavg()
    cpu_after = _cpu_percent_snapshot()

    load_delta = [round(load_after[i] - load_before[i], 3) for i in range(3)]
    cpu_delta = round(cpu_after - cpu_before, 2) if cpu_before is not None and cpu_after is not None else None

    n = n_ok + n_timeout + n_error
    pcts = _percentiles(ok_latencies, [50, 95, 99])

    _emit(
        fh, "summary",
        interval_s=interval_s,
        n=n,
        n_ok=n_ok,
        n_timeout=n_timeout,
        n_error=n_error,
        latency_p50_ms=round(pcts[50], 2) if pcts[50] is not None else None,
        latency_p95_ms=round(pcts[95], 2) if pcts[95] is not None else None,
        latency_p99_ms=round(pcts[99], 2) if pcts[99] is not None else None,
        latency_min_ms=round(min(ok_latencies), 2) if ok_latencies else None,
        latency_max_ms=round(max(ok_latencies), 2) if ok_latencies else None,
        load_delta=load_delta,
        cpu_percent_delta=cpu_delta,
    )

    print(
        f"  interval={interval_s}s  n={n}  ok={n_ok}  timeout={n_timeout}  error={n_error}"
        f"  p50={pcts[50]!r}ms  p99={pcts[99]!r}ms",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    _check_preconditions(cfg.radio.serial_port)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    fh = open(args.out, "a")
    mesh_client = None
    info_start: dict = {}

    try:
        print(f"Connecting to {cfg.radio.serial_port} ...", file=sys.stderr)
        mesh_client = await MeshCore.create_serial(
            cfg.radio.serial_port, 115200, debug=False,
        )
        print("Connected.", file=sys.stderr)

        info_start = _snapshot_self_info(mesh_client)
        _emit(fh, "self_info_start", self_info=info_start)

        mismatches = _check_radio_params(info_start, cfg.radio)
        if mismatches:
            _emit(fh, "event", event="radio_param_mismatch", mismatches=mismatches)
            print(f"WARNING: radio params mismatch: {mismatches}", file=sys.stderr)

        # Prime psutil (first call returns meaningless 0.0)
        _cpu_percent_snapshot()

        intervals = [int(x) for x in args.intervals.split(",")]
        for interval_s in intervals:
            if shutdown_event.is_set():
                break
            print(
                f"Starting interval={interval_s}s for {args.duration_per_interval}s",
                file=sys.stderr,
            )
            await _run_interval(
                mesh_client, interval_s, args.duration_per_interval,
                args.command_timeout, fh, shutdown_event,
            )

    finally:
        if mesh_client is not None:
            try:
                info_end = _snapshot_self_info(mesh_client)
                drift = {
                    k: {"start": info_start.get(k), "end": info_end.get(k)}
                    for k in set(list(info_start.keys()) + list(info_end.keys()))
                    if info_start.get(k) != info_end.get(k)
                }
                _emit(
                    fh, "self_info_end",
                    self_info=info_end,
                    drift=drift,
                )
            except Exception as exc:
                _emit(fh, "event", event="self_info_end_failed", error=str(exc))
            try:
                await mesh_client.disconnect()
                print("Disconnected cleanly.", file=sys.stderr)
            except Exception:
                pass
        fh.close()

    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Measure get_stats_core() latency at configurable poll intervals.",
    )
    p.add_argument(
        "--config", type=str,
        default=str(_REPO_ROOT / "config.toml"),
        help="Path to config.toml (default: %(default)s)",
    )
    p.add_argument(
        "--intervals", type=str, default="10,30,60",
        help="Comma-separated poll intervals in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--duration-per-interval", type=int, default=1200,
        help="Seconds to run each interval (default: %(default)s)",
    )
    p.add_argument(
        "--command-timeout", type=float, default=2.0,
        help="Timeout per get_stats_core() call in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--out", type=str,
        default=str(_HERE / "runs" / "t9_liveness.jsonl"),
        help="Output JSONL file path (default: %(default)s)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
