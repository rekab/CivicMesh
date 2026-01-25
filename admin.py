import argparse
import time
from typing import Callable

from config import load_config
from database import (
    DBConfig,
    cancel_outbox_message,
    clear_pending_outbox,
    get_outbox_message,
    get_pending_outbox_filtered,
    get_recent_messages_filtered,
    get_recent_sessions,
    get_session_by_id,
    init_db,
    pin_message,
    unpin_message,
)
from logger import setup_logging


RECENT_ID_WIDTH = 5
RECENT_TS_WIDTH = 19
RECENT_CHANNEL_WIDTH = 12
RECENT_SOURCE_WIDTH = 4
RECENT_SENDER_WIDTH = 13
RECENT_CONTENT_WIDTH = 50

OUTBOX_ID_WIDTH = 5
OUTBOX_TS_WIDTH = 19
OUTBOX_CHANNEL_WIDTH = 12
OUTBOX_SENDER_WIDTH = 13
OUTBOX_CONTENT_WIDTH = 50

SESSION_LAST_WIDTH = 16
SESSION_NAME_WIDTH = 12
SESSION_LOCATION_WIDTH = 12
SESSION_MAC_WIDTH = 17
SESSION_POSTS_WIDTH = 5


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3]}..."


def _format_recent_messages(rows: list[dict[str, object]]) -> str:
    header = (
        f"{'ID':<{RECENT_ID_WIDTH}} "
        f"{'TS':<{RECENT_TS_WIDTH}} "
        f"{'CH':<{RECENT_CHANNEL_WIDTH}} "
        f"{'SRC':<{RECENT_SOURCE_WIDTH}} "
        f"{'SENDER':<{RECENT_SENDER_WIDTH}} "
        "CONTENT"
    )
    lines = [header]
    for row in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row["ts"])))
        channel = _truncate(str(row["channel"]), RECENT_CHANNEL_WIDTH)
        source = _truncate(str(row["source"]), RECENT_SOURCE_WIDTH)
        sender = _truncate(str(row["sender"]), RECENT_SENDER_WIDTH)
        content = _truncate(str(row["content"]), RECENT_CONTENT_WIDTH)
        lines.append(
            f"{row['id']:<{RECENT_ID_WIDTH}} "
            f"{ts:<{RECENT_TS_WIDTH}} "
            f"{channel:<{RECENT_CHANNEL_WIDTH}} "
            f"{source:<{RECENT_SOURCE_WIDTH}} "
            f"{sender:<{RECENT_SENDER_WIDTH}} "
            f"{content}"
        )
    return "\n".join(lines)


def _format_outbox_messages(rows: list[dict[str, object]]) -> str:
    header = (
        f"{'ID':<{OUTBOX_ID_WIDTH}} "
        f"{'TS':<{OUTBOX_TS_WIDTH}} "
        f"{'CH':<{OUTBOX_CHANNEL_WIDTH}} "
        f"{'SENDER':<{OUTBOX_SENDER_WIDTH}} "
        "CONTENT"
    )
    lines = [header]
    for row in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row["ts"])))
        channel = _truncate(str(row["channel"]), OUTBOX_CHANNEL_WIDTH)
        sender = _truncate(str(row["sender"]), OUTBOX_SENDER_WIDTH)
        content = _truncate(str(row["content"]), OUTBOX_CONTENT_WIDTH)
        lines.append(
            f"{row['id']:<{OUTBOX_ID_WIDTH}} "
            f"{ts:<{OUTBOX_TS_WIDTH}} "
            f"{channel:<{OUTBOX_CHANNEL_WIDTH}} "
            f"{sender:<{OUTBOX_SENDER_WIDTH}} "
            f"{content}"
        )
    return "\n".join(lines)


def _format_sessions(rows: list[dict[str, object]]) -> str:
    session_width = max(len("SESSION"), max((len(str(r.get("session_id", ""))) for r in rows), default=0))
    header = (
        f"{'SESSION':<{session_width}} "
        f"{'LAST':<{SESSION_LAST_WIDTH}} "
        f"{'NAME':<{SESSION_NAME_WIDTH}} "
        f"{'LOC':<{SESSION_LOCATION_WIDTH}} "
        f"{'MAC':<{SESSION_MAC_WIDTH}} "
        f"{'POSTS':<{SESSION_POSTS_WIDTH}}"
    )
    lines = [header]
    for row in rows:
        last_post_ts = row.get("last_post_ts")
        if last_post_ts:
            last_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(last_post_ts)))
        else:
            last_ts = "-"
        session_id = str(row["session_id"])
        name = _truncate(str(row.get("name") or ""), SESSION_NAME_WIDTH)
        location = _truncate(str(row.get("location") or ""), SESSION_LOCATION_WIDTH)
        mac = _truncate(str(row.get("mac_address") or ""), SESSION_MAC_WIDTH)
        posts = str(row.get("post_count_hour") or 0)
        lines.append(
            f"{session_id:<{session_width}} "
            f"{last_ts:<{SESSION_LAST_WIDTH}} "
            f"{name:<{SESSION_NAME_WIDTH}} "
            f"{location:<{SESSION_LOCATION_WIDTH}} "
            f"{mac:<{SESSION_MAC_WIDTH}} "
            f"{posts:<{SESSION_POSTS_WIDTH}}"
        )
    return "\n".join(lines)


def _format_session_detail(row: dict[str, object]) -> str:
    name = str(row.get("name") or "")
    location = str(row.get("location") or "")
    mac = str(row.get("mac_address") or "")
    fingerprint = str(row.get("fingerprint") or "")
    posts = str(row.get("post_count_hour") or 0)
    return "\n".join(
        [
            f"name={name}",
            f"location={location}",
            f"mac={mac}",
            f"post_count_hour={posts}",
            f"fingerprint={fingerprint}",
        ]
    )


def _confirm_outbox_cancel(
    *,
    ts: int,
    sender: str,
    content: str,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
) -> bool:
    ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    print(f"[{ts_str}] <{sender}> {content}")
    if skip_confirmation:
        return True
    resp = input_fn("Cancel this outbox message? [y/N]: ").strip().lower()
    return resp in ("y", "yes")


def _handle_outbox_cancel(
    db_cfg: DBConfig,
    *,
    outbox_id: int,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
    log=None,
) -> bool:
    row = get_outbox_message(db_cfg, outbox_id=outbox_id, log=log)
    if not row:
        print("outbox id not found")
        return False
    if row.get("sent"):
        print("outbox message already sent")
        return False
    if not _confirm_outbox_cancel(
        ts=int(row["ts"]),
        sender=str(row["sender"]),
        content=str(row["content"]),
        skip_confirmation=skip_confirmation,
        input_fn=input_fn,
    ):
        print("canceled=0")
        return False
    canceled = cancel_outbox_message(db_cfg, outbox_id=outbox_id, log=log)
    print(f"canceled={1 if canceled else 0} id={outbox_id}")
    return canceled


def _confirm_outbox_clear(*, skip_confirmation: bool, input_fn: Callable[[str], str]) -> bool:
    if skip_confirmation:
        return True
    resp = input_fn("Cancel all pending outbox messages? [y/N]: ").strip().lower()
    return resp in ("y", "yes")


def _handle_outbox_clear(
    db_cfg: DBConfig,
    *,
    skip_confirmation: bool,
    input_fn: Callable[[str], str],
    log=None,
) -> int:
    if not _confirm_outbox_clear(skip_confirmation=skip_confirmation, input_fn=input_fn):
        print("cleared=0")
        return 0
    cleared = clear_pending_outbox(db_cfg, log=log)
    print(f"cleared={cleared}")
    return cleared


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pin = sub.add_parser("pin")
    p_pin.add_argument("message_id", type=int)
    p_pin.add_argument("--order", type=int, default=None)

    p_unpin = sub.add_parser("unpin")
    p_unpin.add_argument("message_id", type=int)

    p_stats = sub.add_parser("stats")

    p_cleanup = sub.add_parser("cleanup")
    p_cleanup.add_argument("--channel", default=None)

    p_messages = sub.add_parser("messages")
    sub_messages = p_messages.add_subparsers(dest="messages_cmd", required=True)

    p_recent = sub_messages.add_parser("recent")
    p_recent.add_argument("--channel", default=None)
    p_recent.add_argument("--source", choices=["mesh", "wifi"], default=None)
    p_recent.add_argument("--limit", type=int, default=20)

    p_outbox = sub.add_parser("outbox")
    sub_outbox = p_outbox.add_subparsers(dest="outbox_cmd", required=True)

    p_outbox_list = sub_outbox.add_parser("list")
    p_outbox_list.add_argument("--channel", default=None)
    p_outbox_list.add_argument("--limit", type=int, default=20)

    p_outbox_cancel = sub_outbox.add_parser("cancel")
    p_outbox_cancel.add_argument("outbox_id", type=int)
    p_outbox_cancel.add_argument("--skip_confirmation", action="store_true")

    p_outbox_clear = sub_outbox.add_parser("clear")
    p_outbox_clear.add_argument("--skip_confirmation", action="store_true")

    p_sessions = sub.add_parser("sessions")
    sub_sessions = p_sessions.add_subparsers(dest="sessions_cmd", required=True)

    p_sessions_list = sub_sessions.add_parser("list")
    p_sessions_list.add_argument("--limit", type=int, default=20)

    p_sessions_show = sub_sessions.add_parser("show")
    p_sessions_show.add_argument("session_id")

    args = ap.parse_args()

    cfg = load_config(args.config)
    log, _ = setup_logging("admin", cfg.logging)
    log.info("admin:cmd=%s", args.cmd)

    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg, log=log)

    if args.cmd == "pin":
        pin_message(db_cfg, message_id=args.message_id, pin_order=args.order, log=log)
        print(f"Pinned message {args.message_id}")
        return
    if args.cmd == "unpin":
        unpin_message(db_cfg, message_id=args.message_id, log=log)
        print(f"Unpinned message {args.message_id}")
        return
    if args.cmd == "stats":
        # lightweight stats without adding extra DB helpers
        import sqlite3

        conn = sqlite3.connect(cfg.db_path)
        try:
            msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            out = conn.execute("SELECT COUNT(*) FROM outbox WHERE sent=0").fetchone()[0]
            votes = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
        finally:
            conn.close()
        print(f"messages={msg} sessions={sess} outbox_pending={out} votes={votes}")
        return
    if args.cmd == "cleanup":
        # manual retention cleanup
        from database import cleanup_retention_bytes_per_channel

        chans = [args.channel] if args.channel else cfg.channels.names
        total = 0
        for ch in chans:
            d = cleanup_retention_bytes_per_channel(
                db_cfg, channel=ch, max_bytes=cfg.limits.retention_bytes_per_channel, log=log
            )
            total += d
        print(f"deleted={total}")
        return
    if args.cmd == "messages" and args.messages_cmd == "recent":
        rows = get_recent_messages_filtered(
            db_cfg,
            channel=args.channel,
            source=args.source,
            limit=args.limit,
            log=log,
        )
        print(_format_recent_messages(rows))
        return
    if args.cmd == "outbox" and args.outbox_cmd == "list":
        rows = get_pending_outbox_filtered(
            db_cfg,
            channel=args.channel,
            limit=args.limit,
            log=log,
        )
        print(_format_outbox_messages(rows))
        return
    if args.cmd == "outbox" and args.outbox_cmd == "cancel":
        _handle_outbox_cancel(
            db_cfg,
            outbox_id=args.outbox_id,
            skip_confirmation=args.skip_confirmation,
            input_fn=input,
            log=log,
        )
        return
    if args.cmd == "outbox" and args.outbox_cmd == "clear":
        _handle_outbox_clear(
            db_cfg,
            skip_confirmation=args.skip_confirmation,
            input_fn=input,
            log=log,
        )
        return
    if args.cmd == "sessions" and args.sessions_cmd == "list":
        rows = get_recent_sessions(
            db_cfg,
            limit=args.limit,
            log=log,
        )
        print(_format_sessions(rows))
        return
    if args.cmd == "sessions" and args.sessions_cmd == "show":
        row = get_session_by_id(db_cfg, session_id=args.session_id, log=log)
        if not row:
            print("session not found")
            return
        print(_format_session_detail(row))
        return


if __name__ == "__main__":
    main()
