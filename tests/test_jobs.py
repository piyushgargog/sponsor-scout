import shutil
import tempfile
import unittest

from app.db import Database
from app.jobs import Defer, JobQueue, PermanentError
from app.util import iso_ago


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(f"{self.tmp}/j.db")
        self.q = JobQueue(self.db)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, jid):
        return self.db.row("SELECT * FROM jobs WHERE id=?", (jid,))

    def test_lifecycle(self):
        self.q.register("ok", lambda p, j: {"echo": p["x"]})
        jid = self.q.enqueue("ok", {"x": 1})
        self.assertEqual(self.status(jid)["status"], "QUEUED")
        self.assertEqual(self.q.run_all(), 1)
        row = self.status(jid)
        self.assertEqual(row["status"], "COMPLETED")
        self.assertIn("echo", row["result_json"])

    def test_failure_retries_then_fails_and_redacts(self):
        def boom(p, j):
            raise RuntimeError("bad key AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456")
        self.q.register("boom", boom)
        jid = self.q.enqueue("boom", {}, max_attempts=2)
        self.q.run_one()
        self.assertEqual(self.status(jid)["status"], "QUEUED")
        self.db.run("UPDATE jobs SET run_after=?", (iso_ago(seconds=1),))
        self.q.run_one()
        row = self.status(jid)
        self.assertEqual(row["status"], "FAILED")
        self.assertNotIn("AIzaSy", row["error"])

    def test_permanent_error_skips_retries(self):
        def nope(p, j):
            raise PermanentError("no")
        self.q.register("nope", nope)
        jid = self.q.enqueue("nope", {}, max_attempts=5)
        self.q.run_one()
        self.assertEqual(self.status(jid)["status"], "FAILED")

    def test_dedupe_key(self):
        a = self.q.enqueue("t", {}, dedupe_key="k")
        b = self.q.enqueue("t", {}, dedupe_key="k")
        self.assertEqual(a, b)
        self.q.register("t", lambda p, j: None)
        self.q.run_all()
        c = self.q.enqueue("t", {}, dedupe_key="k")  # finished -> new job allowed
        self.assertNotEqual(a, c)

    def test_defer_requeues_without_consuming_attempt(self):
        self.q.register("d", lambda p, j: (_ for _ in ()).throw(Defer(300)))
        jid = self.q.enqueue("d", {})
        self.q.run_one()
        row = self.status(jid)
        self.assertEqual((row["status"], row["attempts"]), ("QUEUED", 0))
        self.assertFalse(self.q.run_one())  # not runnable yet

    def test_unknown_type_fails(self):
        jid = self.q.enqueue("ghost", {})
        self.q.run_one()
        self.assertEqual(self.status(jid)["status"], "FAILED")

    def test_stale_running_recovered_and_summary(self):
        self.q.register("t", lambda p, j: None)
        jid = self.q.enqueue("t", {}, event_id=7)
        self.q.claim()
        self.db.run("UPDATE jobs SET started_at=?", (iso_ago(minutes=30),))
        self.q.recover_stale(15)
        self.assertEqual(self.status(jid)["status"], "QUEUED")
        self.assertEqual(self.q.summary(7)["QUEUED"], 1)

    def test_claim_is_exclusive(self):
        self.q.enqueue("t", {})
        self.assertIsNotNone(self.q.claim())
        self.assertIsNone(self.q.claim())


if __name__ == "__main__":
    unittest.main()
