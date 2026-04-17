from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


HARNESS_VERSION = "0.1.0"


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
