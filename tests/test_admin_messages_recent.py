import tempfile
import time
import unittest

from admin import (
    RECENT_CHANNEL_WIDTH,
    RECENT_CONTENT_WIDTH,
    RECENT_ID_WIDTH,
    RECENT_SENDER_WIDTH,
    RECENT_SOURCE_WIDTH,
    RECENT_TS_WIDTH,
    _format_recent_messages,
    _truncate,
)
from database import DBConfig, get_recent_messages_filtered, init_db, insert_message


class AdminRecentMessagesTest(unittest.TestCase):
    def test_recent_messages_filters_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            now = 1_700_000_000
            sender = "Alice@RossHubStation"
            content = "Anyone have water filters? We're running low at the shelter."
            msg_id = insert_message(
                db_cfg,
                ts=now,
                channel="#fremont",
                sender=sender,
                content=content,
                source="wifi",
            )
            insert_message(
                db_cfg,
                ts=now - 10,
                channel="#fremont",
                sender="KF7XYZ",
                content="Copy that, heading over.",
                source="mesh",
            )
            insert_message(
                db_cfg,
                ts=now - 20,
                channel="#puget-sound",
                sender="W7ABC",
                content="Net check: Ross hub online?",
                source="mesh",
            )

            rows = get_recent_messages_filtered(
                db_cfg,
                channel="#fremont",
                source="wifi",
                limit=20,
            )
            self.assertEqual([r["id"] for r in rows], [msg_id])

            output = _format_recent_messages(rows)
            lines = output.splitlines()
            expected_header = (
                f"{'ID':<{RECENT_ID_WIDTH}} "
                f"{'TS':<{RECENT_TS_WIDTH}} "
                f"{'CH':<{RECENT_CHANNEL_WIDTH}} "
                f"{'SRC':<{RECENT_SOURCE_WIDTH}} "
                f"{'SENDER':<{RECENT_SENDER_WIDTH}} "
                "CONTENT"
            )
            self.assertEqual(lines[0], expected_header)
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            self.assertIn(ts_str, lines[1])
            self.assertIn("#fremont", lines[1])
            self.assertIn("wifi", lines[1])
            self.assertIn(_truncate(sender, RECENT_SENDER_WIDTH), lines[1])
            self.assertIn(_truncate(content, RECENT_CONTENT_WIDTH), lines[1])

    def test_recent_messages_limit_sort(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            now = 1_700_000_100
            older_id = insert_message(
                db_cfg,
                ts=now - 60,
                channel="#fremont",
                sender="Older",
                content="Older message",
                source="wifi",
            )
            newest_id = insert_message(
                db_cfg,
                ts=now,
                channel="#fremont",
                sender="Newest",
                content="Newest message",
                source="wifi",
            )

            rows = get_recent_messages_filtered(db_cfg, channel=None, source=None, limit=1)
            self.assertEqual([r["id"] for r in rows], [newest_id])
            self.assertNotIn(older_id, [r["id"] for r in rows])


if __name__ == "__main__":
    unittest.main()
