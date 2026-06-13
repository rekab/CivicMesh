"""DM command surface for the CivicMesh node (CIV-14).

When a registered contact DMs the node, mesh_bot's CONTACT_MSG_RECV
handler routes the first token through this module. Commands are tiny
on purpose — the goal is a useful status surface walk-up users can
query from their phone, not a chat interface.

This module owns:
  - DMRateLimiter — in-memory per-pubkey sliding-hour rate limit.
    Lost on process restart (no DB persistence); a relay restart
    legitimately resets all per-user budgets.
  - build_*_reply — pure string builders. Take dicts of state, return
    the reply body. No I/O, no logging — testable in isolation.
  - dispatch_command — picks the right builder for a token.

All replies aim for ~150 characters so they fit in a small number of
LoRa packets; the firmware will chunk longer ones but each packet
costs airtime on a shared channel.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional


class DMRateLimiter:
    """Sliding-hour rate limit keyed by pubkey.

    CIV-99: uses time.monotonic() so an admin clock jump or NTP step
    cannot retroactively grant or revoke budget — the cap is "N DMs in
    the last 3600 monotonic seconds," not "N DMs since some wall ts."

    try_consume() is atomic check+record: a granted call appends a
    timestamp before returning True. The deque is bounded by per_hour
    (anything older than the window is popped on entry), so memory per
    pubkey is O(per_hour) in the worst case.
    """

    def __init__(self, per_hour: int):
        if per_hour < 1:
            raise ValueError(f"per_hour must be >= 1, got {per_hour}")
        self._per_hour = per_hour
        self._sends: dict[str, deque[float]] = defaultdict(deque)

    def try_consume(self, pubkey: str, now_mono: Optional[float] = None) -> bool:
        if now_mono is None:
            now_mono = time.monotonic()
        d = self._sends[pubkey]
        cutoff = now_mono - 3600.0
        while d and d[0] < cutoff:
            d.popleft()
        if len(d) >= self._per_hour:
            return False
        d.append(now_mono)
        return True

    def remaining(self, pubkey: str, now_mono: Optional[float] = None) -> int:
        """Number of DMs left in the current sliding hour for this pubkey,
        AFTER current usage. Used by the `stats` reply to show the user
        their own quota. Does NOT consume a token."""
        if now_mono is None:
            now_mono = time.monotonic()
        d = self._sends[pubkey]
        cutoff = now_mono - 3600.0
        while d and d[0] < cutoff:
            d.popleft()
        return max(0, self._per_hour - len(d))

    @property
    def per_hour(self) -> int:
        return self._per_hour


def _fmt_uptime(uptime_s: Optional[int]) -> str:
    if uptime_s is None:
        return "?"
    s = max(0, int(uptime_s))
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h >= 24:
        d, h2 = divmod(h, 24)
        return f"{d}d{h2}h"
    return f"{h}h{m:02d}m"


def _fmt_temp(t: Optional[float]) -> str:
    return "?" if t is None else f"{t:.0f}c"


def _fmt_load(l: Optional[float]) -> str:
    return "?" if l is None else f"{l:.2f}"


def _fmt_disk(free_kb: Optional[int], total_kb: Optional[int]) -> str:
    """Percent of disk free, e.g. '78% free'. '?' if telemetry is missing.

    A genuinely full disk (free_kb == 0) must render '0% free', not '?',
    since that's the exact condition an operator is querying for — so the
    guard tests `is None`, not falsiness.
    """
    if free_kb is None or total_kb is None or total_kb <= 0:
        return "?"
    return f"{round(100 * free_kb / total_kb)}% free"


def _fmt_radio(radio: dict[str, Any]) -> str:
    """Compact companion-radio line, e.g. 'rf rssi-92 snr6.0 q0 nf-115'.

    rssi/nf are dBm, snr is dB, q is the TX queue depth. Each field shows
    '?' when that sample is missing (no radio_samples row yet, or the field
    was null). Bare numbers (no spaces inside a field) so it stays parseable.
    """
    rssi = radio.get("last_rssi")
    snr = radio.get("last_snr")
    queue = radio.get("tx_queue_len")
    noise = radio.get("noise_floor")
    rssi_s = "?" if rssi is None else f"{int(rssi)}"
    snr_s = "?" if snr is None else f"{snr:.1f}"
    q_s = "?" if queue is None else f"{int(queue)}"
    nf_s = "?" if noise is None else f"{int(noise)}"
    return f"rf rssi{rssi_s} snr{snr_s} q{q_s} nf{nf_s}"


def build_help_reply(site_name: str) -> str:
    """Reply to `help`. Short enough for one packet."""
    return (
        f"CivicMesh @ {site_name}\n"
        "help — this message\n"
        "stats — node + traffic"
    )


def build_stats_reply(
    *,
    site_name: str,
    stats: dict[str, Any],
    dm_remaining: int,
    dm_per_hour: int,
) -> str:
    """Reply to `stats`. `stats` is the compute_dm_stats() result.

    The `rts` line (radio hard-resets from failed health checks) is the
    highest-priority addition; the `rf` radio line follows. This pushes the
    reply past the ~150-char/1-packet target — the firmware chunks it into
    ~2 packets, which is an accepted cost for surfacing radio health over DM.
    """
    msgs = stats.get("msgs_sent") or {}
    sess = stats.get("wifi_sessions") or {}
    rts = stats.get("rts_resets") or {}
    radio = stats.get("radio") or {}
    return (
        f"CivicMesh @ {site_name}\n"
        f"up {_fmt_uptime(stats.get('uptime_s'))} "
        f"cpu {_fmt_temp(stats.get('cpu_temp_c'))} "
        f"load {_fmt_load(stats.get('load_1m'))} "
        f"disk {_fmt_disk(stats.get('disk_free_kb'), stats.get('disk_total_kb'))}\n"
        f"rts 1h:{rts.get('1h', 0)} 24h:{rts.get('24h', 0)} "
        f"7d:{rts.get('7d', 0)}\n"
        f"{_fmt_radio(radio)}\n"
        f"msg 1h:{msgs.get('1h', 0)} 24h:{msgs.get('24h', 0)} "
        f"7d:{msgs.get('7d', 0)}\n"
        f"sess 1h:{sess.get('1h', 0)} 24h:{sess.get('24h', 0)} "
        f"7d:{sess.get('7d', 0)}\n"
        f"you: {dm_remaining}/{dm_per_hour} dms/hr left"
    )


def build_unknown_reply() -> str:
    return "unknown command. send 'help' for commands."


def parse_command_token(body: str) -> str:
    """Return the first whitespace-delimited token, lowercased, stripped.

    Empty string if body has nothing usable. Punctuation that doubles as
    a sentence trailing character (`!`, `?`, `.`, `,`) is stripped from
    the right side so `stats?` matches `stats`.
    """
    if not body:
        return ""
    first = body.strip().split(None, 1)[0] if body.strip() else ""
    return first.lower().rstrip("?!.,;:")


# Dispatch table. Each entry returns the reply string given a context
# dict carrying everything a builder might need: site_name, stats,
# dm_remaining, dm_per_hour. Builders pull only what they use.
def dispatch_command(token: str, ctx: dict[str, Any]) -> str:
    """Pick the right builder for `token` and return the reply text.

    `ctx` shape:
      {
        "site_name": str,
        "stats": dict (from compute_dm_stats),  # only used by stats
        "dm_remaining": int,                     # only used by stats
        "dm_per_hour": int,                      # only used by stats
      }
    """
    if token == "help":
        return build_help_reply(ctx["site_name"])
    if token == "stats":
        return build_stats_reply(
            site_name=ctx["site_name"],
            stats=ctx["stats"],
            dm_remaining=ctx["dm_remaining"],
            dm_per_hour=ctx["dm_per_hour"],
        )
    return build_unknown_reply()
