"""Node-side diagnostic script. Runs on each radio node over SSH stdin.

All "command" methods in the meshcore_py library live under mc.commands.*
(e.g. mc.commands.send_chan_msg, mc.commands.get_bat). Instance-level
methods (subscribe, start_auto_message_fetching, disconnect) stay on mc.

Emits JSONL events to stdout and simultaneously to
/tmp/civicmesh_harness_<runid>.jsonl so a broken SSH stream can be recovered
post-hoc. Library debug/log output is routed to stderr.

This script is read as text by the Mac-side orchestrator and piped to
`python3 -u -` on the node — it is never imported.
"""

import os
import sys

# Save fd 1 BEFORE anything imports modules that might print banners or
# trace output. JSONL writes go to this saved fd; everything else (prints,
# logging) is redirected to stderr.
_JSONL_FD = os.dup(1)
os.dup2(2, 1)

import asyncio  # noqa: E402
import base64  # noqa: E402
import io  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

try:
    import tomllib  # type: ignore  # noqa: E402
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore  # noqa: E402


logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


try:
    PARAMS = json.loads(os.environ["RT_PARAMS"])
except Exception as _e:  # pragma: no cover
    sys.stderr.write(f"fatal: bad RT_PARAMS: {_e!r}\n")
    raise SystemExit(2)

NODE_NAME = PARAMS["node_name"]
RUN_ID = PARAMS["run_id"]
SERIAL_PORT = PARAMS["serial_port"]
BAUD = int(PARAMS["baud"])
ROLE = PARAMS["role"]  # listen | send_once | send_repeat
LISTEN_AFTER_S = float(PARAMS["listen_after_s"])
PRE_SEND_DELAY_S = float(PARAMS.get("pre_send_delay_s", 2.0))
MARKER = PARAMS.get("marker", "")
CHANNEL_NAME = PARAMS["channel_name"]
CONFIG_PATH = PARAMS["config_path"]
SEND_SPACING_S = float(PARAMS.get("send_spacing_s", 3.0))
ITERATIONS = int(PARAMS.get("iterations", 1))
STALL_THRESHOLD_S = float(PARAMS.get("stall_threshold_s", 30.0))

_TMP_PATH = f"/tmp/civicmesh_harness_{RUN_ID}.jsonl"
_TMP_FILE = open(_TMP_PATH, "wb")

_seq_counter = itertools.count()
_last_event_mono = time.monotonic()
_stall_emitted = False


def _utc_iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _json_safe(obj, depth=0):
    if depth > 12:
        return "<depth-limit>"
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return {"__bytes_b64__": base64.b64encode(obj).decode("ascii"), "len": len(obj)}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in obj.items()}
    # Enum-like (has .name) → take .name
    name = getattr(obj, "name", None)
    if isinstance(name, str) and name:
        return {"__enum__": name, "__repr__": repr(obj)[:200]}
    # Object with .payload (library event objects)
    payload = getattr(obj, "payload", None)
    if payload is not None:
        inner = _json_safe(payload, depth + 1)
        return {"__event_wrapper__": type(obj).__name__, "payload": inner}
    try:
        return {"__repr__": repr(obj)[:500]}
    except Exception as e:
        return f"<unreprable: {e!r}>"


def emit(event_type, payload=None, **extra):
    global _last_event_mono
    _last_event_mono = time.monotonic()
    rec = {
        "wall_ts": _utc_iso_now(),
        "mono_ts": _last_event_mono,
        "node": NODE_NAME,
        "seq": next(_seq_counter),
        "event_type": event_type,
        "payload": _json_safe(payload if payload is not None else {}),
    }
    if extra:
        rec["extra"] = _json_safe(extra)
    try:
        line = (json.dumps(rec, default=str) + "\n").encode("utf-8")
    except Exception as e:
        line = (json.dumps({
            "wall_ts": rec["wall_ts"],
            "mono_ts": rec["mono_ts"],
            "node": NODE_NAME,
            "seq": rec["seq"],
            "event_type": "EMIT_ENCODE_ERROR",
            "payload": {"original": event_type, "err": repr(e)},
        }) + "\n").encode("utf-8")
    try:
        os.write(_JSONL_FD, line)
    except Exception as e:
        sys.stderr.write(f"jsonl write failed: {e!r}\n")
    try:
        _TMP_FILE.write(line)
        _TMP_FILE.flush()
    except Exception as e:
        sys.stderr.write(f"jsonl tmp-file write failed: {e!r}\n")


def _try_meshcore_version():
    try:
        import meshcore  # type: ignore
        return getattr(meshcore, "__version__", "unknown")
    except Exception as e:
        return f"error: {e!r}"


def _make_handler(et):
    def _h(*args, **kwargs):
        try:
            event_obj = args[0] if args else None
            payload = getattr(event_obj, "payload", None) if event_obj is not None else None
            if payload is None and event_obj is not None:
                payload = {"__raw_event__": _json_safe(event_obj)}
            emit(
                et.name if hasattr(et, "name") else str(et),
                payload,
                handler_args=len(args),
                handler_kwargs=list(kwargs.keys()),
            )
        except Exception as e:
            emit(
                "HANDLER_ERROR",
                {
                    "event_type": getattr(et, "name", str(et)),
                    "err": repr(e),
                    "tb": traceback.format_exc(limit=4),
                },
            )
    return _h


async def _collect_stats(mc):
    """Pre/post stats probe. All command methods live on mc.commands.* —
    never on mc directly. Graceful fallback if any method is missing in
    this library version."""
    out = {}
    for name in ("send_device_query", "get_bat"):
        fn = getattr(getattr(mc, "commands", None), name, None)
        if fn is None:
            out[name] = "not_available"
            continue
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
            out[name] = _json_safe(result)
        except Exception as e:
            out[name] = {"error": repr(e)}

    send_cmd = getattr(getattr(mc, "commands", None), "send_cmd", None)
    if send_cmd is None:
        out["send_cmd"] = "not_available"
    else:
        # Try common binary stats probes. The meshcore companion protocol
        # uses a command byte (56) with a sub-command (0=core, 1=radio,
        # 2=packet) per the firmware reference. Signature uncertainty is
        # real — catch every exception and record "error" rather than
        # crashing the whole harness. If the call signature differs in
        # this library version, the error message will tell us so.
        for label, code, sub in (("core", 56, 0), ("radio", 56, 1), ("packet", 56, 2)):
            key = f"send_cmd_{label}"
            try:
                result = send_cmd(bytes([code, sub]))
                if asyncio.iscoroutine(result):
                    result = await result
                out[key] = _json_safe(result)
            except TypeError as e:
                out[key] = {"error": f"TypeError (signature mismatch?): {e!r}"}
            except Exception as e:
                out[key] = {"error": repr(e)}
    return out


async def _listen_until(deadline_mono: float, label: str):
    global _stall_emitted
    while time.monotonic() < deadline_mono:
        await asyncio.sleep(0.5)
        if (
            not _stall_emitted
            and time.monotonic() - _last_event_mono > STALL_THRESHOLD_S
        ):
            _stall_emitted = True
            emit("RADIO_STALLED", {
                "silence_s": time.monotonic() - _last_event_mono,
                "phase": label,
            })


async def _do_send(mc, channel_idx: int, marker: str):
    pre_wall = _utc_iso_now()
    pre_mono = time.monotonic()
    emit("SEND_PRE", {
        "marker": marker,
        "channel_idx": channel_idx,
        "pre_wall_ts": pre_wall,
        "pre_mono_ts": pre_mono,
    })
    try:
        # Command methods live under mc.commands — never on the instance.
        result = await mc.commands.send_chan_msg(channel_idx, marker)
        post_mono = time.monotonic()
        result_type = getattr(result, "type", None)
        emit("SEND_RESULT", {
            "marker": marker,
            "channel_idx": channel_idx,
            "type": getattr(result_type, "name", None) or (repr(result_type) if result_type is not None else None),
            "payload": _json_safe(getattr(result, "payload", None)),
            "pre_mono_ts": pre_mono,
            "post_mono_ts": post_mono,
            "post_wall_ts": _utc_iso_now(),
            "duration_s": post_mono - pre_mono,
        })
    except Exception as e:
        emit("SEND_EXCEPTION", {
            "marker": marker,
            "channel_idx": channel_idx,
            "err": repr(e),
            "tb": traceback.format_exc(limit=6),
        })


async def main():
    emit("HARNESS_START", {
        "role": ROLE,
        "listen_after_s": LISTEN_AFTER_S,
        "pre_send_delay_s": PRE_SEND_DELAY_S,
        "serial_port": SERIAL_PORT,
        "baud": BAUD,
        "channel_name": CHANNEL_NAME,
        "config_path": CONFIG_PATH,
        "run_id": RUN_ID,
        "marker": MARKER,
        "iterations": ITERATIONS,
        "send_spacing_s": SEND_SPACING_S,
        "stall_threshold_s": STALL_THRESHOLD_S,
        "meshcore_version": _try_meshcore_version(),
        "python_version": sys.version,
    })

    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        channel_names = list(cfg.get("channels", {}).get("names", []))
    except Exception as e:
        emit("CONFIG_LOAD_ERROR", {"err": repr(e), "config_path": CONFIG_PATH})
        emit("TEST_COMPLETE", {"ok": False, "reason": "config_load_error"})
        return

    if CHANNEL_NAME not in channel_names:
        emit("CHANNEL_NOT_FOUND", {
            "channel_name": CHANNEL_NAME,
            "available": channel_names,
        })
        emit("TEST_COMPLETE", {"ok": False, "reason": "channel_not_found"})
        return

    channel_idx = channel_names.index(CHANNEL_NAME)

    try:
        from meshcore import EventType, MeshCore  # type: ignore
    except Exception as e:
        emit("MESHCORE_IMPORT_ERROR", {"err": repr(e)})
        emit("TEST_COMPLETE", {"ok": False, "reason": "meshcore_import_error"})
        return

    event_type_names = sorted([e.name for e in EventType])
    emit("HARNESS_INIT", {
        "event_types": event_type_names,
        "event_types_count": len(event_type_names),
        "channel_names": channel_names,
        "channel_idx": channel_idx,
        "node_name": NODE_NAME,
    })

    try:
        mc = await MeshCore.create_serial(SERIAL_PORT, BAUD, debug=True)
    except Exception as e:
        emit("CREATE_SERIAL_ERROR", {
            "err": repr(e),
            "tb": traceback.format_exc(limit=6),
        })
        emit("TEST_COMPLETE", {"ok": False, "reason": "create_serial_error"})
        return

    # Record self_info (populated by send_appstart inside create_serial).
    # Used to confirm radio params without ever calling set_radio.
    self_info = getattr(mc, "self_info", None)
    emit("SELF_INFO", _json_safe(self_info or {}))

    emit("PRE_STATS", await _collect_stats(mc))

    # Subscribe to every EventType the library exposes.
    subscribed_ok = 0
    subscribe_errors = []
    for et in EventType:
        try:
            mc.subscribe(et, _make_handler(et))
            subscribed_ok += 1
        except Exception as e:
            subscribe_errors.append({"event_type": et.name, "err": repr(e)})
    emit("SUBSCRIBED", {"ok_count": subscribed_ok, "errors": subscribe_errors})

    try:
        await mc.start_auto_message_fetching()
        emit("AUTO_FETCH_STARTED", {})
    except Exception as e:
        emit("AUTO_FETCH_ERROR", {"err": repr(e)})

    # Role dispatch.
    try:
        if ROLE == "listen":
            deadline = time.monotonic() + LISTEN_AFTER_S
            await _listen_until(deadline, "listen")

        elif ROLE == "send_once":
            if PRE_SEND_DELAY_S > 0:
                await asyncio.sleep(PRE_SEND_DELAY_S)
            await _do_send(mc, channel_idx, MARKER)
            deadline = time.monotonic() + LISTEN_AFTER_S
            await _listen_until(deadline, "post_send_listen")

        elif ROLE == "send_repeat":
            if PRE_SEND_DELAY_S > 0:
                await asyncio.sleep(PRE_SEND_DELAY_S)
            for i in range(ITERATIONS):
                iter_marker = f"{MARKER} i={i} u={uuid.uuid4().hex[:8]}"
                await _do_send(mc, channel_idx, iter_marker)
                if i < ITERATIONS - 1:
                    await asyncio.sleep(SEND_SPACING_S)
            deadline = time.monotonic() + LISTEN_AFTER_S
            await _listen_until(deadline, "post_repeat_listen")

        else:
            emit("UNKNOWN_ROLE", {"role": ROLE})

    except Exception as e:
        emit("ROLE_DISPATCH_ERROR", {
            "role": ROLE,
            "err": repr(e),
            "tb": traceback.format_exc(limit=6),
        })

    emit("POST_STATS", await _collect_stats(mc))

    try:
        disconnect = getattr(mc, "disconnect", None)
        if disconnect is None:
            emit("DISCONNECT_SKIPPED", {"reason": "no disconnect() method"})
        else:
            result = disconnect()
            if asyncio.iscoroutine(result):
                await result
            emit("DISCONNECTED", {})
    except Exception as e:
        emit("DISCONNECT_ERROR", {"err": repr(e)})

    emit("TEST_COMPLETE", {"ok": True})


try:
    asyncio.run(main())
finally:
    try:
        _TMP_FILE.close()
    except Exception:
        pass
