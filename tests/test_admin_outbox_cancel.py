import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from admin import _handle_outbox_cancel
from database import DBConfig, get_pending_outbox_filtered, init_db, queue_outbox


class AdminOutboxCancelTest(unittest.TestCase):
    def test_outbox_cancel_skip_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            ts = 1_700_000_400
            outbox_id = queue_outbox(
                db_cfg,
                ts=ts,
                channel="#fremont",
                sender="Alice",
                content="Please relay this message.",
                session_id="sess-1",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                canceled = _handle_outbox_cancel(
                    db_cfg,
                    outbox_id=outbox_id,
                    skip_confirmation=True,
                    input_fn=lambda _: "y",
                )

            self.assertTrue(canceled)
            self.assertEqual(get_pending_outbox_filtered(db_cfg, channel=None, limit=10), [])
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            self.assertIn(f"[{ts_str}] <Alice> Please relay this message.", buf.getvalue())

    def test_outbox_cancel_confirmation_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            db_cfg = DBConfig(path=db_path)
            init_db(db_cfg)

            ts = 1_700_000_500
            outbox_id = queue_outbox(
                db_cfg,
                ts=ts,
                channel="#fremont",
                sender="Bob",
                content="Hold on canceling this.",
                session_id="sess-2",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                canceled = _handle_outbox_cancel(
                    db_cfg,
                    outbox_id=outbox_id,
                    skip_confirmation=False,
                    input_fn=lambda _: "n",
                )

            self.assertFalse(canceled)
            self.assertEqual(len(get_pending_outbox_filtered(db_cfg, channel=None, limit=10)), 1)
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            self.assertIn(f"[{ts_str}] <Bob> Hold on canceling this.", buf.getvalue())
            self.assertIn("canceled=0", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
