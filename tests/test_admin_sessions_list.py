import tempfile
import time
import unittest

from admin import SESSION_LAST_WIDTH, SESSION_LOCATION_WIDTH, SESSION_MAC_WIDTH, SESSION_NAME_WIDTH, SESSION_POSTS_WIDTH, _format_sessions
from database import DBConfig, get_recent_sessions, init_db, insert_session


class AdminSessionsListTest(unittest.TestCase):
    def test_sessions_list_order_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            insert_session(
                db_cfg,
                session_id="sess-older",
                name="Older",
                location="Dock",
                mac_address="AA:BB:CC:DD:EE:01",
                fingerprint=None,
                created_ts=1_700_000_000,
                last_post_ts=1_700_000_100,
                post_count_hour=1,
            )
            insert_session(
                db_cfg,
                session_id="sess-newer",
                name="Newer",
                location="Hall",
                mac_address="AA:BB:CC:DD:EE:02",
                fingerprint=None,
                created_ts=1_700_000_200,
                last_post_ts=1_700_000_300,
                post_count_hour=3,
            )

            rows = get_recent_sessions(db_cfg, limit=1)
            self.assertEqual([r["session_id"] for r in rows], ["sess-newer"])

            output = _format_sessions(rows)
            lines = output.splitlines()
            expected_header = (
                f"{'SESSION':<{len('sess-newer')}} "
                f"{'LAST':<{SESSION_LAST_WIDTH}} "
                f"{'NAME':<{SESSION_NAME_WIDTH}} "
                f"{'LOC':<{SESSION_LOCATION_WIDTH}} "
                f"{'MAC':<{SESSION_MAC_WIDTH}} "
                f"{'POSTS':<{SESSION_POSTS_WIDTH}}"
            )
            self.assertEqual(lines[0], expected_header)
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(1_700_000_300))
            self.assertIn(ts_str, lines[1])
            self.assertIn("sess-newer", lines[1])


if __name__ == "__main__":
    unittest.main()
