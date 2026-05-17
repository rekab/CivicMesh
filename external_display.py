"""Build the /api/external-display/state v2 payload.

Pure function: given a loaded AppConfig + DBConfig + a "now" timestamp,
returns the dict that the endpoint serializes. Kept out of web_server.py
so it can be unit-tested without a running HTTP server.

See docs/external-display-api.md for the v2 schema.
"""
from __future__ import annotations

import re
import unicodedata
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
_PER_CHANNEL_LIMIT = 5

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


def _channel_payload(db_cfg: DBConfig, name: str, scope: str) -> dict[str, Any]:
    # include_pinned=True + limit=5 returns ALL pinned rows + up to 5 unpinned.
    # Slice to enforce the hard 5-total cap. Pinned ordering inside the helper
    # is pin_order ASC NULLS LAST, ts DESC — preserved by the slice.
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
                "sender": _normalize_text(r["sender"], _SENDER_MAX),
                "body": _normalize_text(r["content"], _BODY_MAX),
            }
            for r in rows
        ],
    }


def build_state(cfg: AppConfig, db_cfg: DBConfig, *, now: int) -> dict[str, Any]:
    """Build the v2 payload. `now` is an epoch-seconds int captured by the
    caller (passed in so tests can pin a deterministic value)."""
    channels = [_channel_payload(db_cfg, n, "local") for n in cfg.local.names]
    channels.extend(_channel_payload(db_cfg, n, "mesh") for n in cfg.channels.names)
    return {
        "api_version": API_VERSION,
        "server_time": now,
        "hub": {
            "site_name": cfg.node.site_name,
            "callsign": cfg.node.callsign,
        },
        "channels": channels,
    }
