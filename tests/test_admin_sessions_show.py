import tempfile
import unittest

from admin import _format_session_detail
from database import DBConfig, get_session_by_id, init_db, insert_session


class AdminSessionsShowTest(unittest.TestCase):
    def test_sessions_show_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            insert_session(
                db_cfg,
                session_id="sess-1",
                name="Alice",
                location="Ross",
                mac_address="AA:BB:CC:DD:EE:FF",
                fingerprint="finger-1",
                created_ts=1_700_000_700,
                last_post_ts=1_700_000_710,
                post_count_hour=4,
            )

            row = get_session_by_id(db_cfg, session_id="sess-1")
            self.assertIsNotNone(row)
            output = _format_session_detail(row or {})
            self.assertIn("name=Alice", output)
            self.assertIn("location=Ross", output)
            self.assertIn("mac=AA:BB:CC:DD:EE:FF", output)
            self.assertIn("post_count_hour=4", output)
            self.assertIn("fingerprint=finger-1", output)


if __name__ == "__main__":
    unittest.main()
