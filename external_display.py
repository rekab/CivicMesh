"""Build the /api/external-display/state v2 payload.

Pure function: given a loaded AppConfig + DBConfig + a "now" timestamp,
returns the dict that the endpoint serializes. Kept out of web_server.py
so it can be unit-tested without a running HTTP server.

See docs/external-display-api.md for the v2 schema.
"""
from __future__ import annotations

import datetime
import re
import unicodedata
import zoneinfo
from typing import Any

from config import AppConfig
from database import DBConfig, get_messages


# Whitespace collapse runs FIRST so tab/newline/CR/VT/FF (all in \s and all
# in 0x00-0x1F) become a space instead of being deleted. After that the only
# whitespace left is 0x20 (out of the strip range), so the control strip can
# use the simple [\x00-\x1F\x7F] without losing word boundaries. Real mesh
# messages can have embedded newlines (mesh_bot._on_channel_message doesn't
# strip them); "Greenwood\nLibrary" must come out as "Greenwood Library".
_WS_RUN_RE = re.compile(r"\s+")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1F\x7F]")

_BODY_MAX = 500
_SENDER_MAX = 64
# Per-channel cap on returned messages. Sized for the Inkplate bulletin
# renderer, which packs messages newest-on-bottom and stops when the pane
# fills. 15 gives the firmware enough headroom that an unexpectedly tall
# wrap (longer body, larger sender, future denser layouts) doesn't leave
# the bottom of the pane empty for want of one more row to draw.
_PER_CHANNEL_LIMIT = 15

API_VERSION = 2


def _normalize_text(s: str, max_len: int) -> str:
    """Fold to ASCII for the Inkplate's ASCII-only font, normalize whitespace,
    drop remaining control chars, cap length. Hard truncate — no ellipsis
    (firmware layout adds its own if needed)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", errors="ignore").decode("ascii")
    s = _WS_RUN_RE.sub(" ", s)        # \t \n \r \v \f and runs of space → " "
    s = _CTRL_CHARS_RE.sub("", s)     # remaining C0 + DEL; 0x20 is out of range
    s = s.strip()
    return s[:max_len]


def _format_ts(ts: int, tz: zoneinfo.ZoneInfo) -> str:
    """Format an epoch-seconds value as HH:MM in the hub's configured tz.

    Per UI_SPEC §5, timestamps on the Inkplate are server-provided
    pre-formatted strings — the firmware does not compute time. Doing
    the conversion here means DST is handled by the Pi's zoneinfo
    database, and a fleet of hubs in different zones can render their
    own wall-clock times without rebuilding firmware.
    """
    return datetime.datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")


def _channel_payload(db_cfg: DBConfig,
                     name: str,
                     scope: str,
                     tz: zoneinfo.ZoneInfo) -> dict[str, Any]:
    # include_pinned=True returns ALL pinned rows + up to limit unpinned.
    # Slice enforces the hard per-channel cap on the combined result.
    # Pinned ordering inside the helper is pin_order ASC NULLS LAST,
    # ts DESC — preserved by the slice.
    rows = get_messages(
        db_cfg,
        channel=name,
        viewer_session_id=None,
        limit=_PER_CHANNEL_LIMIT,
        include_pinned=True,
    )[:_PER_CHANNEL_LIMIT]
    return {
        "name": name,
        "scope": scope,
        "messages": [
            {
                "id": r["id"],
                "ts": r["ts"],
                "ts_str": _format_ts(r["ts"], tz),
                "sender": _normalize_text(r["sender"], _SENDER_MAX),
                "body": _normalize_text(r["content"], _BODY_MAX),
            }
            for r in rows
        ],
    }


def build_state(cfg: AppConfig, db_cfg: DBConfig, *, now: int) -> dict[str, Any]:
    """Build the v2 payload. `now` is an epoch-seconds int captured by the
    caller (passed in so tests can pin a deterministic value)."""
    # cfg.node.timezone is validated at config load time, so ZoneInfo
    # cannot fail here — but cache the lookup once per build so each
    # message doesn't pay the OS-level zoneinfo open.
    tz = zoneinfo.ZoneInfo(cfg.node.timezone)
    channels = [_channel_payload(db_cfg, n, "local", tz) for n in cfg.local.names]
    channels.extend(_channel_payload(db_cfg, n, "mesh", tz) for n in cfg.channels.names)
    return {
        "api_version": API_VERSION,
        "server_time": now,
        "hub": {
            "site_name": cfg.node.site_name,
            "callsign": cfg.node.callsign,
        },
        "channels": channels,
    }
