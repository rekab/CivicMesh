from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logger import NodeConfig, NodeEventSink, node_dir


NODE_SIDE_PATH = Path(__file__).parent / "node_side.py"


def load_node_side_source() -> str:
    return NODE_SIDE_PATH.read_text()


def _ssh_cmd(node: NodeConfig, remote_shell_cmd: str) -> list[str]:
    activate = f"source {shlex.quote(node.repo_path + '/.venv/bin/activate')}"
    inner = f"{activate} && {remote_shell_cmd}"
    return [
        "ssh",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        node.ssh_target,
        inner,
    ]


def _ssh_raw_cmd(node: NodeConfig, remote_shell_cmd: str) -> list[str]:
    """Like _ssh_cmd but without venv activation — for probes."""
    return [
        "ssh",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        node.ssh_target,
        remote_shell_cmd,
    ]


@dataclass
class NodeResult:
    node_name: str
    exit_code: int
    events: list[dict]
    events_path: Path
    stderr_path: Path
    authoritative_path: Path | None
    authoritative_event_count: int | None
    streamed_line_count: int
    hardkilled: bool
    stream_loss: bool
    test_complete: bool
    runid: str


async def _pipe_reader(stream: asyncio.StreamReader, sink: NodeEventSink) -> None:
    while True:
        line = await stream.readline()
        if not line:
            break
        sink.write_raw(line)


async def _stderr_reader(stream: asyncio.StreamReader, path: Path) -> None:
    fh = open(path, "wb")
    try:
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            fh.write(chunk)
            fh.flush()
    finally:
        fh.close()


async def run_node(
    node: NodeConfig,
    params: dict,
    run_dir: Path,
    hard_deadline_s: float,
) -> NodeResult:
    """Launch node_side.py on the node over SSH and stream its JSONL stdout
    to events.jsonl, its stderr (library debug + logging) to meshcore_debug.log."""
    nd = node_dir(run_dir, node.name)
    events_path = nd / "events.jsonl"
    stderr_path = nd / "meshcore_debug.log"
    runid = params["run_id"]

    rt_params_json = json.dumps(params)
    rt_params_quoted = shlex.quote(rt_params_json)
    remote = f"RT_PARAMS={rt_params_quoted} python3 -u -"
    cmd = _ssh_cmd(node, remote)

    source = load_node_side_source()

    sink = NodeEventSink(events_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(source.encode("utf-8"))
    try:
        await proc.stdin.drain()
    except Exception:
        pass
    try:
        proc.stdin.close()
    except Exception:
        pass

    stdout_task = asyncio.create_task(_pipe_reader(proc.stdout, sink))
    stderr_task = asyncio.create_task(_stderr_reader(proc.stderr, stderr_path))

    hardkilled = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=hard_deadline_s)
    except asyncio.TimeoutError:
        hardkilled = True
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    await stdout_task
    await stderr_task
    sink.close()

    test_complete = any(
        isinstance(ev.parsed, dict) and ev.parsed.get("event_type") == "TEST_COMPLETE"
        for ev in sink.events
    )

    # Post-run: fetch authoritative JSONL from /tmp on the node, compare counts
    authoritative_path: Path | None = None
    authoritative_count: int | None = None
    stream_loss = False
    try:
        authoritative_path = nd / "authoritative.jsonl"
        await fetch_node_jsonl(node, runid, authoritative_path)
        authoritative_count = sum(
            1 for line in authoritative_path.read_text(errors="replace").splitlines() if line.strip()
        )
        if authoritative_count > sink.line_count:
            stream_loss = True
    except Exception:
        authoritative_path = None
        authoritative_count = None

    return NodeResult(
        node_name=node.name,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        events=[ev.parsed for ev in sink.events if ev.parsed is not None],
        events_path=events_path,
        stderr_path=stderr_path,
        authoritative_path=authoritative_path,
        authoritative_event_count=authoritative_count,
        streamed_line_count=sink.line_count,
        hardkilled=hardkilled,
        stream_loss=stream_loss,
        test_complete=test_complete,
        runid=runid,
    )


async def fetch_node_jsonl(node: NodeConfig, runid: str, dest: Path) -> None:
    remote_path = f"/tmp/civicmesh_harness_{runid}.jsonl"
    cmd = _ssh_raw_cmd(node, f"cat {shlex.quote(remote_path)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"fetch_node_jsonl failed on {node.name}: "
            f"exit={proc.returncode} stderr={stderr.decode(errors='replace').strip()[:300]}"
        )
    dest.write_bytes(stdout)


async def probe_clock_skew(node: NodeConfig) -> dict:
    remote = "python3 -c 'import time; print(time.time())'"
    cmd = _ssh_raw_cmd(node, remote)
    t0_wall = time.time()
    t0_mono = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    t1_mono = time.monotonic()
    t1_wall = time.time()
    if proc.returncode != 0:
        return {
            "node": node.name,
            "ok": False,
            "err": stderr.decode(errors="replace").strip()[:300],
        }
    try:
        node_ts = float(stdout.decode().strip())
    except Exception as e:
        return {"node": node.name, "ok": False, "err": f"parse: {e!r}"}
    mac_mid_wall = (t0_wall + t1_wall) / 2.0
    rtt_ms = (t1_mono - t0_mono) * 1000.0
    estimated_skew_ms = (node_ts - mac_mid_wall) * 1000.0
    return {
        "node": node.name,
        "ok": True,
        "rtt_ms": rtt_ms,
        "estimated_skew_ms": estimated_skew_ms,
        "mac_mid_wall": mac_mid_wall,
        "node_wall": node_ts,
    }
