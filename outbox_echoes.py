"""In-memory index of recently-sent outbox rows whose mesh echoes we
should still credit toward heard_count.

Background: meshcore_py decrypts incoming channel messages via
`set_decrypt_channel_logs(True)`. For each RX_LOG_DATA event with a
populated `message` field, we want to know whether it's a rebroadcast
of one of our own recent outbox sends, and if so, increment
heard_count on that row.

Matching key: (channel, decrypted_message_text, sender_timestamp).
The message text we store here is the FULL on-the-wire form
"Name: Text" — which is exactly what we'll see come back in
RX_LOG_DATA.payload.message. This means the handler does no string
manipulation beyond a dict lookup.

Lifetime: entries expire after `lifetime_s` seconds (default 20s,
informed by T4's max-observed-echo-offset of 12s + 5s safety + a few
extra for headroom). Late events that don't match an active entry are
silently dropped — undercount over false-positive (project memory).

Concurrency: this is single-threaded (asyncio event loop callbacks).
No locking needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


DEFAULT_LIFETIME_S = 20.0


@dataclass
class _Entry:
    outbox_id: int
    expiry_mono: float


class ActiveOutboxIndex:
    """Maps (channel, full_message_text, sender_ts) -> outbox_id for
    recently-sent rows. Add on send, match on incoming RX_LOG_DATA,
    evict on expiry."""

    def __init__(self, lifetime_s: float = DEFAULT_LIFETIME_S):
        self._entries: dict[tuple[str, str, int], _Entry] = {}
        self._lifetime_s = lifetime_s

    def add(self, *, outbox_id: int, channel: str, expected_text: str, sender_ts: int) -> None:
        """Register an outbound send. `expected_text` is the full
        "Name: Text" string that will appear in incoming
        RX_LOG_DATA.payload.message when this packet is rebroadcast."""
        self._evict_expired()
        key = (channel, expected_text, int(sender_ts))
        self._entries[key] = _Entry(
            outbox_id=int(outbox_id),
            expiry_mono=time.monotonic() + self._lifetime_s,
        )

    def match(self, *, channel: str, message_text: str, sender_ts: int) -> int | None:
        """Look up an outbox_id for an incoming RX_LOG_DATA event.

        Tries sender_ts exact, then ±1s — the firmware-stamped
        sender_timestamp can land in the second after our recorded
        send moment if the firmware processes the enqueue across a
        second boundary. Tighter than T4's ±2s tolerance because the
        full message_text already disambiguates collisions.

        Returns None if no match or the matching entry has expired.
        """
        ts = int(sender_ts)
        now = time.monotonic()
        for candidate_ts in (ts, ts - 1, ts + 1):
            entry = self._entries.get((channel, message_text, candidate_ts))
            if entry is None:
                continue
            if now > entry.expiry_mono:
                # Stale. Leave for evict_expired() to remove in bulk.
                continue
            return entry.outbox_id
        return None

    def _evict_expired(self) -> None:
        now = time.monotonic()
        stale = [k for k, e in self._entries.items() if now > e.expiry_mono]
        for k in stale:
            del self._entries[k]

    def __len__(self) -> int:
        return len(self._entries)
