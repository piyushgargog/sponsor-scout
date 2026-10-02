import io
import logging
import shutil
import tempfile
import unittest

from app.config import Settings
from app.llm.base import LLMProvider
from app.logging_setup import JsonFormatter
from app.services import build_services

SECRET = "t" * 40


def make_settings(tmp, **kw):
    base = dict(database_url=f"sqlite:///{tmp}/test.db", worker_enabled=False, admin_password="pw", secret_key=SECRET,
                send_interval_seconds=0, log_level="CRITICAL", llm_provider="mock", mail_provider="mock", search_provider="mock")
    base.update(kw)
    return Settings(**base)


class AppCase(unittest.TestCase):
    settings_kw = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = make_settings(self.tmp, **self.settings_kw)
        self.svc = build_services(self.settings, **getattr(self, "svc_kw", {}))
        self.repo, self.db, self.pipe, self.sender = self.svc.repo, self.svc.db, self.svc.pipeline, self.svc.sender
        self.event_id = self.repo.create_event(EVENT, self.settings.campaign_daily_limit, "test")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pipeline(self):
        self.pipe.discover(self.event_id)
        self.svc.queue.run_all()

    def lead_by_name(self, name):
        return next(l for l in self.repo.list_leads(self.event_id) if l["company_name"] == name)

    def connect_demo(self):
        self.svc.accounts.save("mock", "demo@mock.local", "a", "r", "2999-01-01T00:00:00Z", "s")

    def approved_draft(self, name):
        lead = self.lead_by_name(name)
        did = self.pipe.generate_email(lead["id"])
        d = self.repo.get_draft(did)
        self.sender.approve(did, "tester", acknowledge=d["status"] == "NEEDS_REVIEW")
        return did, lead


EVENT = {"name": "XYZ Tech Fest 2026", "college": "XYZ University", "city": "Delhi", "country": "India", "event_date": "2026-11-20",
         "expected_attendance": 500, "event_type": "technology", "audience": ["B.Tech students", "AI/ML students", "developers", "startup enthusiasts"],
         "requirements": ["cash sponsorship", "API credits", "cloud credits", "swag", "speakers", "workshop partners"],
         "description": "", "benefits": "", "categories": [], "keywords": []}


class LogCapture:
    def __enter__(self):
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setFormatter(JsonFormatter())
        lg = logging.getLogger("sponsor")
        lg.addHandler(self.h)
        self.old = lg.level
        lg.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        lg = logging.getLogger("sponsor")
        lg.removeHandler(self.h)
        lg.setLevel(self.old)

    @property
    def text(self):
        return self.buf.getvalue()


class ScriptedLLM(LLMProvider):
    """Returns canned JSON per task; falls back to the mock provider otherwise."""
    name = "scripted"
    model = "scripted-1"

    def __init__(self, overrides=None):
        from app.llm.mock import MockProvider
        self.mock, self.overrides, self.calls = MockProvider(), overrides or {}, []

    def generate(self, prompt, **kw):
        return "x"

    def generate_json(self, prompt, *, system=None, schema=None, task=None, context=None, temperature=0.2):
        self.calls.append(task)
        ov = self.overrides.get(task)
        if ov is not None:
            if isinstance(ov, Exception):
                raise ov
            return ov(context) if callable(ov) else ov
        return self.mock.generate_json(prompt, task=task, context=context)
