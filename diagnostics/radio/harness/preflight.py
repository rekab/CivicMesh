from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from .logger import NodeConfig, console_log


PREFLIGHT_PY_TEMPLATE = r"""
import json, os, subprocess

checks = {}
me = os.getpid()

def _read_cmdline(pid):
    try:
        with open('/proc/' + str(pid) + '/cmdline', 'rb') as f:
            raw = f.read()
    except Exception:
        return None
    parts = [p.decode('utf-8', 'replace') for p in raw.split(b'\x00') if p]
    return parts

def _iter_pids():
    try:
        return [int(e) for e in os.listdir('/proc') if e.isdigit()]
    except FileNotFoundError:
        return []

# Find real mesh_bot.py invocations — argv contains mesh_bot.py as a file
# argument, NOT embedded in a `-c` inline script body (which would
# self-match this very preflight process).
mesh_bot_matches = []
for pid in _iter_pids():
    if pid == me:
        continue
    argv = _read_cmdline(pid)
    if not argv:
        continue
    skip_next = False
    hit = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in ('-c', '--command'):
            skip_next = True
            continue
        if a == 'mesh_bot.py' or a.endswith('/mesh_bot.py'):
            hit = True
            break
    if hit:
        mesh_bot_matches.append(str(pid) + ' ' + ' '.join(argv))
checks['mesh_bot_pids'] = '\n'.join(mesh_bot_matches)

# Find processes holding the serial port by walking /proc/*/fd symlinks.
# More reliable than lsof (which isn't installed on minimal Debian).
serial_port = __SERIAL_PORT__
real_target = os.path.realpath(serial_port) if os.path.exists(serial_port) else serial_port
serial_holders = []
for pid in _iter_pids():
    if pid == me:
        continue
    fd_dir = '/proc/' + str(pid) + '/fd'
    try:
        fds = os.listdir(fd_dir)
    except Exception:
        continue
    held = False
    for fd in fds:
        try:
            link = os.readlink(fd_dir + '/' + fd)
        except Exception:
            continue
        if link == serial_port or link == real_target:
            held = True
            break
        try:
            if os.path.realpath(link) == real_target:
                held = True
                break
        except Exception:
            pass
    if held:
        argv = _read_cmdline(pid) or []
        serial_holders.append(str(pid) + ' ' + ' '.join(argv))
checks['serial_holders'] = '\n'.join(serial_holders)

checks['serial_exists'] = os.path.exists(serial_port)
checks['serial_readable'] = os.access(serial_port, os.R_OK)

try:
    import meshcore  # type: ignore
    checks['meshcore'] = 'ok'
    checks['meshcore_version'] = getattr(meshcore, '__version__', 'unknown')
except Exception as e:
    checks['meshcore'] = 'error: ' + repr(e)

try:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    with open(__CONFIG_PATH__, 'rb') as f:
        cfg = tomllib.load(f)
    checks['channels'] = cfg.get('channels', {}).get('names', [])
except Exception as e:
    checks['channels'] = []
    checks['config_error'] = 'error: ' + repr(e)

try:
    r = subprocess.run(['chronyc', 'tracking'], capture_output=True, text=True)
    checks['chronyc'] = r.stdout if r.returncode == 0 else ('error: ' + r.stderr.strip())
except FileNotFoundError:
    checks['chronyc'] = 'not installed'
except Exception as e:
    checks['chronyc'] = 'error: ' + repr(e)

print(json.dumps(checks))
"""


def _build_preflight_script(node: NodeConfig) -> str:
    script = PREFLIGHT_PY_TEMPLATE
    script = script.replace("__SERIAL_PORT__", repr(node.serial_port))
    script = script.replace("__CONFIG_PATH__", repr(f"{node.repo_path}/config.toml"))
    return script


def _remote_command(node: NodeConfig, remote_shell_cmd: str) -> list[str]:
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


@dataclass
class PreflightResult:
    node_name: str
    ok: bool
    failures: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    channel_names: list[str] = field(default_factory=list)
    channel_idx: int | None = None
    raw_checks: dict[str, Any] = field(default_factory=dict)
    chronyc_offset_ms: float | None = None

    def summary_line(self) -> str:
        status = "OK" if self.ok else "FAIL"
        bits = [f"{self.node_name}: {status}"]
        if self.failures:
            bits.append(" / ".join(self.failures))
        if self.annotations:
            bits.append("notes: " + " / ".join(self.annotations))
        return " | ".join(bits)


_CHRONY_OFFSET_RE = re.compile(r"Last offset\s*:\s*([\-0-9\.eE+]+)\s*seconds?")


def _parse_chrony_offset_ms(text: str) -> float | None:
    if not isinstance(text, str):
        return None
    m = _CHRONY_OFFSET_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1)) * 1000.0
    except Exception:
        return None


async def preflight(node: NodeConfig, channel_name: str) -> PreflightResult:
    console_log(f"mac→{node.name}", f"preflight: ssh {node.ssh_target} checking mesh_bot/serial/meshcore/channel/clock")
    script = _build_preflight_script(node)
    remote_cmd = f"python3 -c {shlex.quote(script)}"
    cmd = _remote_command(node, remote_cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    result = PreflightResult(node_name=node.name, ok=False)

    if proc.returncode != 0:
        result.failures.append(
            f"ssh/preflight exited {proc.returncode}: "
            f"{stderr.decode(errors='replace').strip()[:400]}"
        )
        return result

    try:
        checks = json.loads(stdout.decode())
    except Exception as e:
        result.failures.append(f"preflight output not JSON: {e!r}; raw={stdout!r}")
        return result

    result.raw_checks = checks

    pids = checks.get("mesh_bot_pids") or ""
    if isinstance(pids, str) and pids.strip():
        result.failures.append(f"mesh_bot still running: {pids.strip()[:300]}")
    holders = checks.get("serial_holders") or ""
    if isinstance(holders, str) and holders.strip():
        result.failures.append(
            f"{node.serial_port} held by another process: {holders.strip()[:300]}"
        )
    if not checks.get("serial_exists"):
        result.failures.append(f"{node.serial_port} does not exist on {node.name}")
    if not checks.get("serial_readable"):
        result.failures.append(f"{node.serial_port} not readable by user on {node.name}")
    if checks.get("meshcore") != "ok":
        result.failures.append(
            f"meshcore library not importable in venv: {checks.get('meshcore')}"
        )
    channels = checks.get("channels") or []
    result.channel_names = list(channels) if isinstance(channels, list) else []
    if channel_name not in result.channel_names:
        result.failures.append(
            f"channel {channel_name!r} not in node's config.toml channels: "
            f"{result.channel_names}"
        )
    else:
        result.channel_idx = result.channel_names.index(channel_name)

    chrony_text = checks.get("chronyc")
    offset_ms = _parse_chrony_offset_ms(chrony_text) if isinstance(chrony_text, str) else None
    result.chronyc_offset_ms = offset_ms
    if offset_ms is not None and abs(offset_ms) > 100:
        result.annotations.append(
            f"clock offset {offset_ms:+.1f}ms (>100ms — latency math has wide error bars)"
        )
    if chrony_text == "not installed":
        result.annotations.append("chronyc not installed — clock offset unknown")

    result.ok = not result.failures
    status = "OK" if result.ok else "FAIL"
    console_log(f"mac→{node.name}", f"preflight: {status}  ({result.summary_line()})")
    return result
