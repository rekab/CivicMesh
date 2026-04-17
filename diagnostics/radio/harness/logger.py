from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


HARNESS_VERSION = "0.1.0"


def console_log(source: str, msg: str) -> None:
    """Write a timestamped, source-prefixed line to the orchestrator console.

    source examples: "mac", "mac→pi4", "pi4", "zero2w".
    """
    now = datetime.now(timezone.utc).astimezone()
    ts = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
    print(f"{ts} [{source}] {msg}", flush=True)


# Events streamed from the node side that are worth echoing to the Mac
# console in real time. Keeps noise low — RX_LOG_DATA and similar are
# excluded (they're in events.jsonl for later analysis).
LIVE_ECHO_EVENTS = frozenset({
    "HARNESS_START",
    "HARNESS_INIT",
    "SELF_INFO",
    "PRE_STATS",
    "POST_STATS",
    "SUBSCRIBED",
    "AUTO_FETCH_STARTED",
    "AUTO_FETCH_ERROR",
    "CREATE_SERIAL_ERROR",
    "MESHCORE_IMPORT_ERROR",
    "CONFIG_LOAD_ERROR",
    "CHANNEL_NOT_FOUND",
    "CHANNEL_QUERY_PRE",
    "CHANNEL_QUERY_PRE_ERROR",
    "CHANNEL_QUERY_POST",
    "CHANNEL_QUERY_POST_ERROR",
    "SET_CHANNEL_PRE",
    "SET_CHANNEL_RESULT",
    "SET_CHANNEL_ERROR",
    "SET_CHANNEL_SKIPPED",
    "SEND_PRE",
    "SEND_RESULT",
    "SEND_EXCEPTION",
    "RADIO_STALLED",
    "CHANNEL_MSG_RECV",
    "HANDLER_ERROR",
    "DISCONNECTED",
    "DISCONNECT_ERROR",
    "TEST_COMPLETE",
    "UNKNOWN_ROLE",
    "ROLE_DISPATCH_ERROR",
    "EMIT_ENCODE_ERROR",
})


def format_event_oneline(ev: dict) -> str:
    """Compact one-line summary of a JSONL event for live echo."""
    et = ev.get("event_type", "?")
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    bits = [et]
    if et == "HARNESS_START":
        bits.append(f"role={payload.get('role')}")
        bits.append(f"listen_after_s={payload.get('listen_after_s')}")
    elif et == "HARNESS_INIT":
        types_count = payload.get("event_types_count")
        bits.append(f"event_types={types_count}")
        bits.append(f"channel_idx={payload.get('channel_idx')}")
    elif et == "SUBSCRIBED":
        bits.append(f"ok_count={payload.get('ok_count')}")
        errs = payload.get("errors") or []
        if errs:
            bits.append(f"errors={len(errs)}")
    elif et == "SEND_PRE":
        marker = payload.get("marker") or ""
        bits.append(f'marker="{_truncate(marker, 60)}"')
    elif et == "SEND_RESULT":
        bits.append(f"type={payload.get('type')}")
        inner = payload.get("payload")
        if isinstance(inner, dict) and payload.get("type") == "ERROR":
            bits.append(f"reason={inner.get('reason')}")
        dur = payload.get("duration_s")
        if isinstance(dur, (int, float)):
            bits.append(f"duration={dur*1000:.0f}ms")
    elif et == "SEND_EXCEPTION":
        bits.append(f'err={_truncate(str(payload.get("err")), 80)}')
    elif et == "CHANNEL_MSG_RECV":
        sender = payload.get("sender") or payload.get("pubkey_prefix") or "?"
        text = payload.get("text") or payload.get("content") or ""
        bits.append(f'sender="{_truncate(str(sender), 20)}"')
        bits.append(f'text="{_truncate(str(text), 60)}"')
        pl = payload.get("path_len")
        if pl is not None:
            bits.append(f"path_len={pl}")
        snr = payload.get("SNR")
        if snr is not None:
            bits.append(f"SNR={snr}")
        sts = payload.get("sender_timestamp")
        if sts is not None:
            bits.append(f"sender_ts={sts}")
    elif et in ("CHANNEL_QUERY_PRE", "CHANNEL_QUERY_POST"):
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        bits.append(f"idx={payload.get('channel_idx')}")
        bits.append(f"result_type={payload.get('type')}")
        # Try to surface the secret/hash/key if present — common key names
        # vary across library versions; show whichever appears.
        for key in ("secret", "psk", "hash", "key", "channel_hash", "channel_secret"):
            v = inner.get(key)
            if v is not None:
                bits.append(f"{key}={_truncate(str(v), 40)}")
                break
    elif et == "SET_CHANNEL_PRE":
        bits.append(f"idx={payload.get('channel_idx')}")
        bits.append(f"name={payload.get('channel_name')}")
        bits.append(f"secret_hex={_truncate(str(payload.get('secret_hex')), 32)}")
    elif et == "SET_CHANNEL_RESULT":
        bits.append(f"idx={payload.get('channel_idx')}")
        bits.append(f"result_type={payload.get('type')}")
    elif et == "SET_CHANNEL_ERROR":
        bits.append(f'err={_truncate(str(payload.get("err")), 120)}')
    elif et == "SET_CHANNEL_SKIPPED":
        bits.append(f"reason={payload.get('reason')}")
    elif et == "RADIO_STALLED":
        bits.append(f"silence_s={payload.get('silence_s'):.1f}" if isinstance(payload.get("silence_s"), (int, float)) else "")
        bits.append(f"phase={payload.get('phase')}")
    elif et in ("CREATE_SERIAL_ERROR", "MESHCORE_IMPORT_ERROR", "CONFIG_LOAD_ERROR",
                "AUTO_FETCH_ERROR", "DISCONNECT_ERROR", "HANDLER_ERROR"):
        bits.append(f'err={_truncate(str(payload.get("err")), 120)}')
    elif et == "CHANNEL_NOT_FOUND":
        bits.append(f"channel_name={payload.get('channel_name')}")
        bits.append(f"available={payload.get('available')}")
    elif et == "TEST_COMPLETE":
        if payload.get("reason"):
            bits.append(f"reason={payload.get('reason')}")
    return " ".join(b for b in bits if b)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


@dataclass
class NodeConfig:
    name: str
    ssh_host: str
    ssh_user: str
    serial_port: str
    baud: int
    repo_path: str

    @property
    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"


@dataclass
class TestConfig:
    channel: str
    message_prefix: str
    min_send_spacing_s: float


@dataclass
class NodesConfig:
    nodes: dict[str, NodeConfig]
    test: TestConfig

    def node(self, name: str) -> NodeConfig:
        if name not in self.nodes:
            raise KeyError(f"no node named {name!r} in nodes.toml (have: {sorted(self.nodes)})")
        return self.nodes[name]


def load_nodes_config(path: Path) -> NodesConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    test_raw = raw.get("test")
    if not test_raw:
        raise ValueError(f"{path}: missing [test] section")
    test = TestConfig(
        channel=test_raw["channel"],
        message_prefix=test_raw["message_prefix"],
        min_send_spacing_s=float(test_raw["min_send_spacing_s"]),
    )
    nodes: dict[str, NodeConfig] = {}
    for key, val in raw.items():
        if key == "test":
            continue
        if not isinstance(val, dict):
            continue
        nodes[key] = NodeConfig(
            name=key,
            ssh_host=val["ssh_host"],
            ssh_user=val["ssh_user"],
            serial_port=val["serial_port"],
            baud=int(val["baud"]),
            repo_path=val["repo_path"],
        )
    if not nodes:
        raise ValueError(f"{path}: no node sections found")
    return NodesConfig(nodes=nodes, test=test)


def utc_iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def run_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def make_run_dir(runs_dir: Path, test_name: str, suffix: str = "") -> Path:
    ts = run_timestamp()
    label = f"{ts}__{test_name}" + (f"_{suffix}" if suffix else "")
    run_dir = runs_dir / label
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def update_latest_symlink(runs_dir: Path, run_dir: Path) -> None:
    link = runs_dir / "latest"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except FileNotFoundError:
        pass
    link.symlink_to(run_dir.name)


def node_dir(run_dir: Path, node_name: str) -> Path:
    d = run_dir / node_name
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class StreamedEvent:
    raw_line: str
    parsed: dict | None
    parse_error: str | None = None


class NodeEventSink:
    """Writes a node's streamed JSONL stdout to events.jsonl and keeps a parsed list."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = open(path, "wb")
        self.events: list[StreamedEvent] = []
        self.line_count = 0

    def write_raw(self, line_bytes: bytes) -> None:
        self._fh.write(line_bytes)
        if not line_bytes.endswith(b"\n"):
            self._fh.write(b"\n")
        self._fh.flush()
        self.line_count += 1
        text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        try:
            parsed = json.loads(text)
            self.events.append(StreamedEvent(raw_line=text, parsed=parsed))
        except Exception as e:
            self.events.append(StreamedEvent(raw_line=text, parsed=None, parse_error=repr(e)))

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def write_manifest(run_dir: Path, manifest: dict) -> None:
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))


def write_summary(run_dir: Path, summary_md: str) -> None:
    (run_dir / "summary.md").write_text(summary_md)


def load_events(path: Path) -> list[dict]:
    """Load JSONL file into a list of parsed dicts. Unparseable lines become
    {'event_type': 'PARSE_ERROR', 'raw': line, 'err': repr(e)}."""
    out: list[dict] = []
    if not path.exists():
        return out
    for raw in path.read_text(errors="replace").splitlines():
        raw = raw.rstrip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception as e:
            out.append({"event_type": "PARSE_ERROR", "raw": raw, "err": repr(e)})
    return out
