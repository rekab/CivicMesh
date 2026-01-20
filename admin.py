import argparse
import time

from config import load_config
from database import DBConfig, init_db, pin_message, unpin_message
from logger import setup_logging


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


if __name__ == "__main__":
    main()

