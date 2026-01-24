import tempfile
import time
import unittest

from admin import (
    OUTBOX_CHANNEL_WIDTH,
    OUTBOX_CONTENT_WIDTH,
    OUTBOX_ID_WIDTH,
    OUTBOX_SENDER_WIDTH,
    OUTBOX_TS_WIDTH,
    _format_outbox_messages,
    _truncate,
)
from database import DBConfig, get_pending_outbox_filtered, init_db, queue_outbox


class AdminOutboxListTest(unittest.TestCase):
    def test_outbox_list_filters_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            now = 1_700_000_200
            sender = "Alice@RossHubStation"
            content = "Outbound relay note that is long enough to truncate."
            outbox_id = queue_outbox(
                db_cfg,
                ts=now,
                channel="#fremont",
                sender=sender,
                content=content,
                session_id="sess-1",
            )
            queue_outbox(
                db_cfg,
                ts=now + 10,
                channel="#local",
                sender="LocalSender",
                content="Local only",
                session_id="sess-2",
            )

            rows = get_pending_outbox_filtered(db_cfg, channel="#fremont", limit=20)
            self.assertEqual([r["id"] for r in rows], [outbox_id])

            output = _format_outbox_messages(rows)
            lines = output.splitlines()
            expected_header = (
                f"{'ID':<{OUTBOX_ID_WIDTH}} "
                f"{'TS':<{OUTBOX_TS_WIDTH}} "
                f"{'CH':<{OUTBOX_CHANNEL_WIDTH}} "
                f"{'SENDER':<{OUTBOX_SENDER_WIDTH}} "
                "CONTENT"
            )
            self.assertEqual(lines[0], expected_header)
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            self.assertIn(ts_str, lines[1])
            self.assertIn("#fremont", lines[1])
            self.assertIn(_truncate(sender, OUTBOX_SENDER_WIDTH), lines[1])
            self.assertIn(_truncate(content, OUTBOX_CONTENT_WIDTH), lines[1])

    def test_outbox_list_limit_sort(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            now = 1_700_000_300
            oldest_id = queue_outbox(
                db_cfg,
                ts=now - 30,
                channel="#fremont",
                sender="Older",
                content="Oldest first",
                session_id="sess-1",
            )
            queue_outbox(
                db_cfg,
                ts=now,
                channel="#fremont",
                sender="Newest",
                content="Newest last",
                session_id="sess-2",
            )

            rows = get_pending_outbox_filtered(db_cfg, channel=None, limit=1)
            self.assertEqual([r["id"] for r in rows], [oldest_id])


if __name__ == "__main__":
    unittest.main()
