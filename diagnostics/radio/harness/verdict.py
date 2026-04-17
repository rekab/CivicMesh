from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterable


def find_send_results(events: Iterable[dict]) -> list[dict]:
    return [ev for ev in events if isinstance(ev, dict) and ev.get("event_type") == "SEND_RESULT"]


def find_send_exceptions(events: Iterable[dict]) -> list[dict]:
    return [ev for ev in events if isinstance(ev, dict) and ev.get("event_type") == "SEND_EXCEPTION"]


def find_channel_msg_recv(events: Iterable[dict]) -> list[dict]:
    return [ev for ev in events if isinstance(ev, dict) and ev.get("event_type") == "CHANNEL_MSG_RECV"]


def event_payload_text(ev: dict) -> str:
    """Pull whatever string content we can from an event payload."""
    payload = ev.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "content", "message", "msg"):
        v = payload.get(key)
        if isinstance(v, str):
            return v
    return ""


def recipient_received_marker(events: Iterable[dict], marker: str) -> dict | None:
    for ev in find_channel_msg_recv(events):
        text = event_payload_text(ev)
        if marker and marker in text:
            return ev
    return None


def classify_send(send_result: dict | None, recipient_match: dict | None) -> str:
    """Return one of:
      BOTH_AGREE_SENT
      BOTH_AGREE_FAILED
      SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED  ← the bug we're hunting
      SENDER_SAYS_SENT_NOT_RECEIVED
      INCONCLUSIVE
    """
    if send_result is None:
        return "INCONCLUSIVE"
    payload = send_result.get("payload") if isinstance(send_result, dict) else None
    sender_type = payload.get("type") if isinstance(payload, dict) else None

    sender_ok = sender_type is not None and sender_type != "ERROR"
    sender_failed = sender_type == "ERROR"
    received = recipient_match is not None

    if sender_ok and received:
        return "BOTH_AGREE_SENT"
    if sender_failed and not received:
        return "BOTH_AGREE_FAILED"
    if sender_failed and received:
        return "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED"
    if sender_ok and not received:
        return "SENDER_SAYS_SENT_NOT_RECEIVED"
    return "INCONCLUSIVE"


def event_type_counts(events: Iterable[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        et = ev.get("event_type")
        if not isinstance(et, str):
            continue
        out[et] = out.get(et, 0) + 1
    return out


def collect_rssi(events: Iterable[dict]) -> list[float]:
    out: list[float] = []
    for ev in events:
        payload = ev.get("payload") if isinstance(ev, dict) else None
        if not isinstance(payload, dict):
            continue
        rssi = payload.get("rssi")
        if isinstance(rssi, (int, float)):
            out.append(float(rssi))
    return out


def percentiles(values: list[float], pcts: list[float]) -> dict[float, float | None]:
    if not values:
        return {p: None for p in pcts}
    sorted_vals = sorted(values)
    out: dict[float, float | None] = {}
    for p in pcts:
        if p <= 0:
            out[p] = sorted_vals[0]
            continue
        if p >= 100:
            out[p] = sorted_vals[-1]
            continue
        # Linear interpolation
        k = (len(sorted_vals) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(sorted_vals) - 1)
        if f == c:
            out[p] = sorted_vals[f]
        else:
            out[p] = sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)
    return out


def median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def parse_iso_to_epoch(ts) -> float | None:
    """Convert ISO8601 ('2026-04-17T21:13:50.129Z') to unix-epoch seconds (float)."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def make_expected_text(sender_name: str, marker: str) -> str:
    """The firmware prepends 'Name: ' during transmission, so a matching
    CHANNEL_MSG_RECV.payload.text on any node is exactly this composite."""
    return f"{sender_name}: {marker}"


def match_self_echo(events: Iterable[dict], expected_text: str,
                    expected_sender_ts_epoch: float, txt_type: int = 0,
                    ts_tolerance_s: float = 2.0) -> list[dict]:
    """Return the list of CHANNEL_MSG_RECV events whose composite key
    (text == expected_text, sender_timestamp within ±ts_tolerance_s of
    expected_sender_ts_epoch, txt_type == txt_type) matches.

    Used to attribute echoes back to a specific outbound send. Bias is
    tight on cross-message disambiguation — late echoes that fail any
    leg of this triple match are dropped rather than misattributed."""
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event_type") != "CHANNEL_MSG_RECV":
            continue
        p = ev.get("payload")
        if not isinstance(p, dict):
            continue
        if p.get("text") != expected_text:
            continue
        if int(p.get("txt_type", 0) or 0) != int(txt_type):
            continue
        ts = p.get("sender_timestamp")
        if not isinstance(ts, (int, float)):
            continue
        if abs(float(ts) - float(expected_sender_ts_epoch)) > ts_tolerance_s:
            continue
        out.append(ev)
    return out


def find_self_info_name(events: Iterable[dict]) -> str | None:
    """Pull the node's `self_info.name` from the first SELF_INFO event."""
    for ev in events:
        if isinstance(ev, dict) and ev.get("event_type") == "SELF_INFO":
            p = ev.get("payload")
            if isinstance(p, dict):
                name = p.get("name")
                if isinstance(name, str) and name:
                    return name
    return None


def manifest_for(test_name: str, marker: str, nodes_cfg, run_id: str, sender: str | None, receiver: str | None) -> dict:
    return {
        "test": test_name,
        "marker": marker,
        "run_id": run_id,
        "channel": nodes_cfg.test.channel,
        "message_prefix": nodes_cfg.test.message_prefix,
        "min_send_spacing_s": nodes_cfg.test.min_send_spacing_s,
        "sender": sender,
        "receiver": receiver,
        "nodes": {
            name: {
                "ssh_host": nc.ssh_host,
                "ssh_user": nc.ssh_user,
                "serial_port": nc.serial_port,
                "baud": nc.baud,
                "repo_path": nc.repo_path,
            }
            for name, nc in nodes_cfg.nodes.items()
        },
    }
