import io
import tempfile
import unittest
from contextlib import redirect_stdout

from admin import _handle_outbox_clear
from database import DBConfig, get_pending_outbox_filtered, init_db, queue_outbox


class AdminOutboxClearTest(unittest.TestCase):
    def test_outbox_clear_skip_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            queue_outbox(
                db_cfg,
                ts=1_700_000_600,
                channel="#fremont",
                sender="Alice",
                content="Pending 1",
                session_id="sess-1",
            )
            queue_outbox(
                db_cfg,
                ts=1_700_000_610,
                channel="#fremont",
                sender="Bob",
                content="Pending 2",
                session_id="sess-2",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                cleared = _handle_outbox_clear(
                    db_cfg,
                    skip_confirmation=True,
                    input_fn=lambda _: "y",
                )

            self.assertEqual(cleared, 2)
            self.assertEqual(get_pending_outbox_filtered(db_cfg, channel=None, limit=10), [])
            self.assertIn("cleared=2", buf.getvalue())

    def test_outbox_clear_confirmation_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            queue_outbox(
                db_cfg,
                ts=1_700_000_620,
                channel="#fremont",
                sender="Carol",
                content="Pending 3",
                session_id="sess-3",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                cleared = _handle_outbox_clear(
                    db_cfg,
                    skip_confirmation=False,
                    input_fn=lambda _: "n",
                )

            self.assertEqual(cleared, 0)
            self.assertEqual(len(get_pending_outbox_filtered(db_cfg, channel=None, limit=10)), 1)
            self.assertIn("cleared=0", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
