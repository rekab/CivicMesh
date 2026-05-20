#!/usr/bin/env python3
"""Host-side iteration harness for the Inkplate bulletin renderer.

Hermetic: no live web server, no live DB, no firmware. The pipeline:

  1. Reset a scratch SQLite DB at /tmp/inkplate-iteration-db.sqlite and
     init the schema via database.init_db (same code path the live DB uses).
  2. Run diagnostics/mesh-sim/inject.py against the scratch DB to populate
     synthetic mesh activity. Default scenario: silent-drift.
  3. Call external_display.build_state directly against the scratch DB to
     produce the v2 payload — no HTTP, no running web server.
  4. For each channel in the payload, set the active-channel index, wrap
     the payload in a stock firmware-side envelope (mirrors render-live.sh),
     and pipe through inkplate/host/host_render to a PNG.
  5. Write a contact sheet at .contact/index.html: thumbnails at ~50%
     scale, captioned with channel name, click for full-size.
  6. Print the path to index.html on stdout.

Edit a screen in inkplate/render/src/screens/, re-run this script, eyeball.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Repo root is two levels up from inkplate/tools/contact-sheet.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from database import DBConfig, init_db  # noqa: E402
from external_display import build_state  # noqa: E402


_SCRATCH_DB = Path("/tmp/inkplate-iteration-db.sqlite")
_CONTACT_DIR = _REPO_ROOT / ".contact"
_INJECT_PY = _REPO_ROOT / "diagnostics" / "mesh-sim" / "inject.py"
_SCENARIO_DIR = _REPO_ROOT / "diagnostics" / "mesh-sim" / "scenarios"
_HOST_RENDER = _REPO_ROOT / "inkplate" / "host" / "host_render"

# Stock firmware-side envelope. Mirrors inkplate/tools/render-live.sh exactly
# — the renderer treats these fields as firmware-injected, so the harness
# must supply them just like the firmware would on a real Inkplate. Per the
# task scope, no flags for tweaking these; if the design loop needs e.g. a
# stale/low-battery contact sheet, that's a future PR.
_STOCK_ENVELOPE = {
    "status": "ok",
    "radio_state": "ok",
    "portal_state": "ok",
    "battery_volts": 3.95,
    "seconds_since_last_update": 60,
    "active_channel_index": 0,
    "failure_reason": None,
    "firmware_version": "0.1.0",
    "expected_api_version": 2,
    "cached_payload": None,
}

# Stock /api/stats subset. The renderer only reads the subset of fields
# stats.cpp parses; the rest of compute_stats's output is dropped here to
# keep the harness file small and the visual mental model clear. Values
# chosen to exercise the §5 nerd strip in a "healthy hub" state — bump
# the values during iteration if a particular range matters.
_STOCK_STATS = {
    "system": {
        "uptime_s": 273_625,  # ~3d 4h
        "cpu": {"load_1m": 0.27, "temp_c": 64.8},
        "mem": {"available_mb": 2011},
        "outbox": {"depth_now": 0},
    },
    "wifi_sessions": {"now": 2, "day": 5, "week": 12},
    "messages_seen": {
        "hour": {
            "bucket_s": 300,
            # 12 buckets of 5-min activity. Mixed shape so the sparkline
            # has actual contour to read; zeros and peaks both visible.
            "bars": [0, 3, 7, 12, 9, 4, 1, 0, 6, 14, 8, 2],
        },
    },
}

# Stock /api/status subset. Same rationale as _STOCK_STATS.
_STOCK_STATUS = {
    "radio_status": "online",
    "age_sec": 8,
}

_DEFAULT_SCENARIO = "silent-drift"


def _reset_scratch_db() -> None:
    # WAL + SHM sidecars must go too — leftover WAL pages from a previous
    # run would replay into the fresh DB and resurrect old messages.
    for suffix in ("", "-shm", "-wal", "-journal"):
        p = Path(str(_SCRATCH_DB) + suffix)
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _resolve_scenario(arg: str) -> Path:
    """Accept either a bare scenario name (e.g. "silent-drift") or a path."""
    p = Path(arg)
    if p.exists():
        return p
    named = _SCENARIO_DIR / f"{arg}.json"
    if named.exists():
        return named
    raise FileNotFoundError(
        f"contact-sheet: scenario {arg!r} not found "
        f"(tried {p} and {named})"
    )


_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(s: str) -> str:
    # Channel names include '#' and arbitrary user input; sanitize for the
    # filesystem and the URL fragment in index.html. Collapse runs of unsafe
    # chars to a single underscore, trim edges.
    cleaned = _SAFE_RE.sub("_", s).strip("_")
    return cleaned or "channel"


def _ensure_host_render() -> None:
    if _HOST_RENDER.exists() and os.access(_HOST_RENDER, os.X_OK):
        return
    print("contact-sheet: host_render not built; running `make`...", file=sys.stderr)
    subprocess.run(["make"], cwd=_HOST_RENDER.parent, check=True)


def _render_channel(envelope_payload: dict, out_path: Path) -> None:
    proc = subprocess.run(
        [str(_HOST_RENDER)],
        input=json.dumps(envelope_payload).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.buffer.write(proc.stderr)
        raise SystemExit(
            f"contact-sheet: host_render failed (exit {proc.returncode}) "
            f"writing {out_path.name}"
        )
    out_path.write_bytes(proc.stdout)


def _write_index(entries: list[tuple[str, str, str]], scenario_name: str) -> Path:
    """Write .contact/index.html. `entries` is [(channel_name, scope, png_filename)]."""
    # Inkplate panel is 800x600; halve for thumbnails. Click opens full PNG
    # in a new tab — keep it dead simple, no JS.
    css = """
    body { font-family: system-ui, sans-serif; background: #222; color: #eee;
           margin: 0; padding: 24px; }
    h1 { font-weight: 400; font-size: 18px; margin: 0 0 4px 0; }
    .meta { color: #888; font-size: 13px; margin-bottom: 24px; }
    .grid { display: flex; flex-wrap: wrap; gap: 24px; }
    figure { margin: 0; background: #111; border: 1px solid #333;
             padding: 8px; border-radius: 4px; }
    figure a { display: block; line-height: 0; }
    figure img { width: 400px; height: 300px; display: block;
                 image-rendering: pixelated; background: white; }
    figcaption { font-size: 13px; color: #ddd; padding-top: 8px;
                 line-height: 1.3; }
    figcaption .scope { color: #888; font-size: 11px; }
    """
    items = []
    for name, scope, fname in entries:
        items.append(
            "<figure>"
            f'<a href="{html.escape(fname)}" target="_blank">'
            f'<img src="{html.escape(fname)}" alt="{html.escape(name)}">'
            "</a>"
            "<figcaption>"
            f"{html.escape(name)}"
            f' <span class="scope">[{html.escape(scope)}]</span>'
            "</figcaption>"
            "</figure>"
        )
    body = (
        f"<h1>contact sheet — scenario {html.escape(scenario_name)!s}</h1>"
        f'<div class="meta">{len(entries)} channel(s); thumbs at 50%, click for full 800x600.</div>'
        f'<div class="grid">{"".join(items)}</div>'
    )
    doc = (
        "<!doctype html>"
        '<html><head><meta charset="utf-8">'
        f"<title>inkplate contact sheet — {html.escape(scenario_name)}</title>"
        f"<style>{css}</style></head>"
        f"<body>{body}</body></html>"
    )
    out = _CONTACT_DIR / "index.html"
    out.write_text(doc, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contact-sheet",
        description=(
            "Hermetic host-side iteration harness for the Inkplate bulletin "
            "renderer. Builds a per-channel PNG contact sheet against a "
            "scratch DB; no web server, no firmware."
        ),
    )
    parser.add_argument(
        "--scenario",
        default=_DEFAULT_SCENARIO,
        help=f"Scenario name (looked up in {_SCENARIO_DIR.relative_to(_REPO_ROOT)}/) "
             "or a path to a scenario JSON file. Default: silent-drift.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.toml. Default: $CIVICMESH_CONFIG or ./config.toml. "
             "Provides channels/limits — db_path is ignored (scratch DB wins).",
    )
    args = parser.parse_args(argv)

    scenario_path = _resolve_scenario(args.scenario)

    _reset_scratch_db()
    db_cfg = DBConfig(path=str(_SCRATCH_DB))
    # init_db is the same code path the live app uses — schema + migrations.
    init_db(db_cfg)

    config_path = args.config or os.environ.get("CIVICMESH_CONFIG") or str(_REPO_ROOT / "config.toml")
    # Load early so a bad config (or [diagnostics] disabled) fails before we
    # bother running the injector.
    cfg = load_config(config_path)

    inject_cmd = [
        sys.executable, str(_INJECT_PY),
        str(scenario_path),
        "--db", str(_SCRATCH_DB),
        "--config", config_path,
    ]
    print(f"contact-sheet: injecting scenario {scenario_path.name} into {_SCRATCH_DB}", file=sys.stderr)
    subprocess.run(inject_cmd, check=True)

    payload = build_state(cfg, db_cfg, now=int(time.time()))
    channels = payload.get("channels", [])

    _ensure_host_render()
    _CONTACT_DIR.mkdir(parents=True, exist_ok=True)
    # Wipe stale PNGs from a previous run so a renamed channel doesn't leave
    # an orphan tile in the contact sheet.
    for old in _CONTACT_DIR.glob("*.png"):
        old.unlink()

    # No-channels payloads still produce one rendered frame so the operator
    # can see the renderer's "No channels configured." takeover.
    if not channels:
        print("contact-sheet: payload has zero channels; rendering empty-state frame", file=sys.stderr)
        envelope = dict(_STOCK_ENVELOPE)
        envelope["active_channel_index"] = 0
        png_path = _CONTACT_DIR / "00_empty.png"
        _render_channel(
            {
                "envelope": envelope,
                "payload": payload,
                "stats": _STOCK_STATS,
                "status": _STOCK_STATUS,
            },
            png_path,
        )
        entries = [("(no channels)", "n/a", png_path.name)]
    else:
        entries: list[tuple[str, str, str]] = []
        for idx, ch in enumerate(channels):
            envelope = dict(_STOCK_ENVELOPE)
            envelope["active_channel_index"] = idx
            png_name = f"{idx:02d}_{_safe_segment(ch.get('name', f'ch{idx}'))}.png"
            png_path = _CONTACT_DIR / png_name
            _render_channel(
                {
                    "envelope": envelope,
                    "payload": payload,
                    "stats": _STOCK_STATS,
                    "status": _STOCK_STATUS,
                },
                png_path,
            )
            entries.append((ch.get("name", "?"), ch.get("scope", "?"), png_name))

    index = _write_index(entries, scenario_path.stem)
    print(index)
    return 0


if __name__ == "__main__":
    sys.exit(main())
