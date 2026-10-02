"""Lightweight database-backed job queue + in-process worker threads.

QUEUED -> RUNNING -> COMPLETED | FAILED. Handlers raise Defer(seconds) to be re-queued later
(used for send throttling) and PermanentError to skip retries.
"""
import threading
import traceback

from .db import Database
from .logging_setup import log_event, redact
from .util import iso_ago, iso_in, jdump, jload, now_iso


class Defer(Exception):
    def __init__(self, seconds: int, reason: str = ""):
        super().__init__(reason or f"deferred {seconds}s")
        self.seconds = seconds


class PermanentError(Exception):
    pass


class JobQueue:
    def __init__(self, db: Database):
        self.db = db
        self.handlers = {}

    def register(self, job_type: str, fn):
        self.handlers[job_type] = fn

    def enqueue(self, job_type: str, payload: dict, *, dedupe_key: str | None = None, delay: int = 0,
                max_attempts: int = 2, event_id: int | None = None, label: str | None = None) -> int:
        with self.db.tx():
            if dedupe_key:
                ex = self.db.row("SELECT id FROM jobs WHERE dedupe_key=? AND status IN ('QUEUED','RUNNING')", (dedupe_key,))
                if ex:
                    return ex["id"]
            return self.db.run(
                "INSERT INTO jobs(type,payload_json,max_attempts,event_id,label,dedupe_key,run_after,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (job_type, jdump(payload), max_attempts, event_id, label, dedupe_key,
                 iso_in(seconds=delay) if delay else now_iso(), now_iso()))

    def claim(self):
        with self.db.tx():
            j = self.db.row("SELECT * FROM jobs WHERE status='QUEUED' AND run_after<=? ORDER BY id LIMIT 1", (now_iso(),))
            if not j:
                return None
            self.db.run("UPDATE jobs SET status='RUNNING', started_at=?, attempts=attempts+1 WHERE id=?", (now_iso(), j["id"]))
            j["attempts"] += 1
            return j

    def run_one(self) -> bool:
        j = self.claim()
        if not j:
            return False
        fn = self.handlers.get(j["type"])
        try:
            if not fn:
                raise PermanentError(f"no handler for job type {j['type']}")
            result = fn(jload(j["payload_json"], {}), j) or {}
            self.db.run("UPDATE jobs SET status='COMPLETED', result_json=?, error=NULL, finished_at=? WHERE id=?",
                        (jdump(result), now_iso(), j["id"]))
        except Defer as d:
            self.db.run("UPDATE jobs SET status='QUEUED', run_after=?, attempts=attempts-1 WHERE id=?",
                        (iso_in(seconds=d.seconds), j["id"]))
        except PermanentError as e:
            self._fail(j, str(e), retry=False)
        except Exception as e:  # noqa: BLE001
            log_event("job_error", job_id=j["id"], type=j["type"], error=redact(str(e)), tb=redact(traceback.format_exc()[-800:]))
            self._fail(j, f"{type(e).__name__}: {e}", retry=True)
        return True

    def _fail(self, j, msg, retry):
        msg = redact(msg)[:500]
        if retry and j["attempts"] < j["max_attempts"]:
            self.db.run("UPDATE jobs SET status='QUEUED', error=?, run_after=? WHERE id=?",
                        (msg, iso_in(seconds=5 * j["attempts"]), j["id"]))
        else:
            self.db.run("UPDATE jobs SET status='FAILED', error=?, finished_at=? WHERE id=?", (msg, now_iso(), j["id"]))

    def run_all(self, max_jobs: int = 2000) -> int:
        """Synchronously drain runnable jobs (tests / CLI). Stops when nothing is runnable now."""
        n = 0
        while n < max_jobs and self.run_one():
            n += 1
        return n

    def recover_stale(self, minutes: int = 15):
        self.db.run("UPDATE jobs SET status='QUEUED' WHERE status='RUNNING' AND started_at<?", (iso_ago(minutes=minutes),))

    def list_for_event(self, event_id: int | None, limit: int = 60):
        if event_id:
            return self.db.rows("SELECT * FROM jobs WHERE event_id=? ORDER BY id DESC LIMIT ?", (event_id, limit))
        return self.db.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))

    def summary(self, event_id: int | None = None) -> dict:
        w, p = ("WHERE event_id=?", (event_id,)) if event_id else ("", ())
        rows = self.db.rows(f"SELECT status, COUNT(*) c FROM jobs {w} GROUP BY status", p)
        out = {"QUEUED": 0, "RUNNING": 0, "COMPLETED": 0, "FAILED": 0}
        out.update({r["status"]: r["c"] for r in rows})
        return out


class Worker:
    def __init__(self, queue: JobQueue, threads: int = 2):
        self.queue, self.n = queue, max(1, threads)
        self._stop = threading.Event()
        self._threads = []

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self.queue.run_one():
                    self._stop.wait(0.7)
            except Exception as e:  # noqa: BLE001
                log_event("worker_error", error=redact(str(e)))
                self._stop.wait(2)

    def start(self):
        self.queue.recover_stale()
        for i in range(self.n):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
