"""Tests for ActiveOutboxIndex.

Covers the matching semantics described in docs/heard_count_design.md
and the duplicate-content negative test in the verification plan: two
outbox rows with identical (channel, text) within the lifetime window
must track their echoes independently."""

import time

from outbox_echoes import ActiveOutboxIndex


def test_basic_match_returns_outbox_id():
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    idx.add(
        outbox_id=42,
        channel="#civicmesh",
        expected_text="oh_no: <alice@hub> hi",
        sender_ts=1000,
    )
    assert idx.match(
        channel="#civicmesh",
        message_text="oh_no: <alice@hub> hi",
        sender_ts=1000,
    ) == 42


def test_no_match_when_text_differs():
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    idx.add(outbox_id=1, channel="#civicmesh", expected_text="oh_no: A", sender_ts=1000)
    assert idx.match(channel="#civicmesh", message_text="oh_no: B", sender_ts=1000) is None


def test_no_match_when_channel_differs():
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    idx.add(outbox_id=1, channel="#civicmesh", expected_text="oh_no: A", sender_ts=1000)
    assert idx.match(channel="#other", message_text="oh_no: A", sender_ts=1000) is None


def test_sender_ts_tolerance_plus_or_minus_one_second():
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    idx.add(outbox_id=7, channel="#x", expected_text="me: hi", sender_ts=1000)
    # exact, ±1
    assert idx.match(channel="#x", message_text="me: hi", sender_ts=1000) == 7
    assert idx.match(channel="#x", message_text="me: hi", sender_ts=999) == 7
    assert idx.match(channel="#x", message_text="me: hi", sender_ts=1001) == 7
    # ±2 should not match
    assert idx.match(channel="#x", message_text="me: hi", sender_ts=998) is None
    assert idx.match(channel="#x", message_text="me: hi", sender_ts=1002) is None


def test_duplicate_content_distinct_sender_ts_tracked_independently():
    """The negative test from heard_count_design.md verification plan:
    two outbox rows with identical (channel, text) within window must
    track their echoes separately, keyed on sender_ts."""
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    idx.add(outbox_id=10, channel="#x", expected_text="me: ack", sender_ts=1000)
    idx.add(outbox_id=11, channel="#x", expected_text="me: ack", sender_ts=1030)
    assert idx.match(channel="#x", message_text="me: ack", sender_ts=1000) == 10
    assert idx.match(channel="#x", message_text="me: ack", sender_ts=1030) == 11
    # Echo at the boundary timestamp goes to the closest entry.
    assert idx.match(channel="#x", message_text="me: ack", sender_ts=1001) == 10


def test_expiry_drops_old_entries():
    idx = ActiveOutboxIndex(lifetime_s=0.05)  # 50ms lifetime
    idx.add(outbox_id=99, channel="#x", expected_text="me: bye", sender_ts=1000)
    assert idx.match(channel="#x", message_text="me: bye", sender_ts=1000) == 99
    time.sleep(0.1)
    # Past lifetime, match returns None
    assert idx.match(channel="#x", message_text="me: bye", sender_ts=1000) is None


def test_same_text_resent_within_window_replaces_outbox_id_for_same_sender_ts():
    """If the same (channel, text, sender_ts) is somehow registered
    twice (e.g. retry), the latest add() wins. Edge case; documented."""
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    idx.add(outbox_id=1, channel="#x", expected_text="me: hi", sender_ts=1000)
    idx.add(outbox_id=2, channel="#x", expected_text="me: hi", sender_ts=1000)
    assert idx.match(channel="#x", message_text="me: hi", sender_ts=1000) == 2


def test_evict_expired_called_on_add():
    idx = ActiveOutboxIndex(lifetime_s=0.05)
    idx.add(outbox_id=1, channel="#x", expected_text="me: a", sender_ts=1000)
    assert len(idx) == 1
    time.sleep(0.1)
    idx.add(outbox_id=2, channel="#x", expected_text="me: b", sender_ts=2000)
    # First entry should have been evicted before the second was added
    assert len(idx) == 1
    assert idx.match(channel="#x", message_text="me: a", sender_ts=1000) is None
    assert idx.match(channel="#x", message_text="me: b", sender_ts=2000) == 2


def test_no_match_when_index_empty():
    idx = ActiveOutboxIndex(lifetime_s=20.0)
    assert idx.match(channel="#x", message_text="anything", sender_ts=1000) is None
