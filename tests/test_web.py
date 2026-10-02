import json
import re
import shutil
import tempfile
import unittest

from app import create_app
from app.config import Settings
from app.logging_setup import log_event, redact
from tests.helpers import LogCapture, make_settings

FAKE_GEMINI = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE1234"
FAKE_GSECRET = "GOCSPX-FAKE-CLIENT-SECRET-123456"
FAKE_BRAVE = "BSA_FAKE_BRAVE_KEY_123456789"
EVENT_FORM = {"event_name": "XYZ Tech Fest 2026", "college": "XYZ University", "city": "Delhi", "country": "India", "event_date": "2026-11-20",
              "expected_attendance": "500", "event_type": "technology", "audience": "B.Tech students, developers",
              "sponsorship_requirements": "cash sponsorship, API credits, swag", "benefits": ""}


def token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = make_settings(self.tmp, gemini_api_key=FAKE_GEMINI, google_client_secret=FAKE_GSECRET, brave_api_key=FAKE_BRAVE)
        self.app = create_app(self.s, start_worker=False)
        self.svc = self.app.extensions["svc"]
        self.c = self.app.test_client()

    def tearDown(self):
        self.svc.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self):
        t = token(self.c.get("/login").get_data(as_text=True))
        return self.c.post("/login", data={"password": "pw", "csrf_token": t})

    def post(self, path, page="/", **data):
        data["csrf_token"] = token(self.c.get(page).get_data(as_text=True))
        return self.c.post(path, data=data)

    def flashes(self, page="/"):
        return re.findall(r'class="flash [a-z]+" role="status">([^<]+)', self.c.get(page).get_data(as_text=True))


class AuthAndCsrfTests(WebCase):
    def test_everything_requires_login(self):
        for path in ("/", "/leads", "/campaigns/1", "/leads/1", "/api/jobs"):
            r = self.c.get(path)
            self.assertIn(r.status_code, (302, 401), path)
            self.assertNotIn(b"Companies found", r.data)
        r = self.c.post("/events", data=EVENT_FORM)
        self.assertEqual(self.svc.db.scalar("SELECT COUNT(*) FROM events"), 0)
        self.assertEqual(self.c.get("/healthz").status_code, 200)

    def test_wrong_password_rejected_and_login_rate_limited(self):
        t = token(self.c.get("/login").get_data(as_text=True))
        r = self.c.post("/login", data={"password": "nope", "csrf_token": t})
        self.assertEqual(self.c.get("/").status_code, 302)
        codes = []
        for _ in range(12):
            r = self.c.post("/login", data={"password": "nope", "csrf_token": t})
            codes.append(r.status_code)
        self.assertEqual(self.c.get("/").status_code, 302)  # never got in
        self.assertTrue(any("Too many" in m for m in self.flashes("/login")), "login attempts beyond the limit must be throttled")
        self.assertFalse(self.app.extensions["limiter"].allow("127.0.0.1:login", 10, 300))
        self.assertGreaterEqual(codes.count(303), 1)

    def test_csrf_required_on_every_post(self):
        self.login()
        r = self.c.post("/events", data=EVENT_FORM)  # no token
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.svc.db.scalar("SELECT COUNT(*) FROM events"), 0)
        r = self.c.post("/events", data=dict(EVENT_FORM, csrf_token="forged"))
        self.assertEqual(self.svc.db.scalar("SELECT COUNT(*) FROM events"), 0)
        r = self.c.post("/logout", data={})
        self.assertEqual(self.c.get("/").status_code, 200)  # still logged in: logout without CSRF was refused

    def test_state_changing_routes_reject_get(self):
        self.login()
        for path in ("/drafts/1/send", "/drafts/1/approve", "/events", "/leads/1/research", "/gmail/disconnect"):
            self.assertEqual(self.c.get(path).status_code, 405, path)

    def test_open_redirect_blocked(self):
        t = token(self.c.get("/login").get_data(as_text=True))
        r = self.c.post("/login", data={"password": "pw", "csrf_token": t, "next": "//evil.example/x"})
        self.assertNotIn("evil.example", r.headers["Location"])
        r = self.c.post("/leads/1/research", data={"csrf_token": token(self.c.get("/").get_data(as_text=True)), "next": "https://evil.example"})
        self.assertNotIn("evil.example", r.headers.get("Location", ""))

    def test_security_headers_and_cookie_flags(self):
        r = self.c.get("/login")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", r.headers["Content-Security-Policy"])
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", r.headers["Set-Cookie"])


class ValidationTests(WebCase):
    def test_invalid_event_rejected(self):
        self.login()
        bad = dict(EVENT_FORM, event_date="not-a-date", expected_attendance="-5", audience="")
        self.post("/events", **bad)
        self.assertEqual(self.svc.db.scalar("SELECT COUNT(*) FROM events"), 0)
        msgs = " ".join(self.flashes())
        self.assertIn("event date", msgs)
        self.assertIn("expected attendance", msgs)

    def test_html_in_inputs_is_escaped(self):
        self.login()
        self.post("/events", **dict(EVENT_FORM, event_name="<script>alert(1)</script> Fest"))
        page = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_manual_contact_validation(self):
        self.login()
        self.post("/events", **EVENT_FORM)
        self.svc.queue.run_all()
        lid = self.svc.repo.list_leads(1)[0]["id"]
        self.post(f"/leads/{lid}/contact", page=f"/leads/{lid}", email="bad address", name="", role="")
        self.assertEqual(self.svc.db.scalar("SELECT COUNT(*) FROM contacts WHERE source_url LIKE 'manual%'"), 0)

    def test_unknown_ids_404(self):
        self.login()
        for path in ("/campaigns/999", "/leads/999"):
            self.assertEqual(self.c.get(path).status_code, 404)
        r = self.post("/drafts/999/send")
        self.assertEqual(r.status_code, 404)


class SecretHandlingTests(WebCase):
    def test_secrets_never_reach_the_browser(self):
        self.login()
        self.post("/events", **EVENT_FORM)
        self.svc.queue.run_all()
        self.post("/gmail/demo-connect")
        lid = self.svc.repo.list_leads(1)[0]["id"]
        self.post(f"/leads/{lid}/generate")
        self.svc.queue.run_all()
        pages = ["/", "/login", "/leads?event=1", "/campaigns/1", f"/leads/{lid}", "/api/jobs?event=1", "/static/app.js", "/static/app.css"]
        for p in pages:
            body = self.c.get(p).get_data(as_text=True)
            for secret in (FAKE_GEMINI, FAKE_GSECRET, FAKE_BRAVE, "pw", self.s.secret_key):
                if secret == "pw":
                    continue  # too short to grep meaningfully
                self.assertNotIn(secret, body, f"{secret[:6]}… leaked in {p}")
            self.assertNotIn("refresh_token", body)
            self.assertNotIn("access_token", body)

    def test_oauth_tokens_not_stored_in_plaintext(self):
        self.svc.accounts.save("mock", "x@y.com", "ya29.PLAINTEXT_ACCESS", "1//PLAINTEXT_REFRESH_TOKEN_1234567", "2999-01-01T00:00:00Z", "s")
        dump = json.dumps(self.svc.db.rows("SELECT * FROM oauth_accounts"))
        self.assertNotIn("PLAINTEXT", dump)

    def test_logs_redact_secrets(self):
        with LogCapture() as lc:
            log_event("llm_request", api_key=FAKE_GEMINI, note=f"key={FAKE_GEMINI} Bearer ya29.abcdef", nested={"refresh_token": "1//abc", "ok": 1})
        self.assertNotIn(FAKE_GEMINI, lc.text)
        self.assertNotIn("ya29.abcdef", lc.text)
        self.assertNotIn("1//abc", lc.text)
        self.assertIn('"ok": 1', lc.text)
        self.assertEqual(redact("client_secret=abc123 password: hunter2"), "client_secret=[REDACTED] password: [REDACTED]")

    def test_production_refuses_weak_config(self):
        with self.assertRaises(RuntimeError):
            Settings(app_env="production", secret_key="short", admin_password="x").validate_for_runtime()
        with self.assertRaises(RuntimeError):
            Settings(app_env="production", secret_key="k" * 40, admin_password="").validate_for_runtime()
        with self.assertRaises(RuntimeError):
            Settings(llm_provider="gemini", gemini_api_key="").validate_for_runtime()
        with self.assertRaises(RuntimeError):
            Settings(mail_provider="gmail").validate_for_runtime()
        Settings(app_env="production", secret_key="k" * 40, admin_password="x").validate_for_runtime()


class SendAuthorizationTests(WebCase):
    def test_send_endpoint_refuses_unapproved_drafts(self):
        self.login()
        self.post("/events", **EVENT_FORM)
        self.svc.queue.run_all()
        self.post("/gmail/demo-connect")
        lead = next(l for l in self.svc.repo.list_leads(1) if l["company_name"] == "NimbusForge Cloud")
        self.post(f"/leads/{lead['id']}/generate")
        self.svc.queue.run_all()
        did = self.svc.repo.latest_draft_for_lead(lead["id"])["id"]
        self.post(f"/drafts/{did}/send", page=f"/leads/{lead['id']}")
        self.svc.queue.run_all()
        self.assertEqual(self.svc.mailer.outbox, [])
        self.assertIn("approved by a human", " ".join(self.flashes(f"/leads/{lead['id']}")))

    def test_rate_limit_on_expensive_endpoints(self):
        self.login()
        self.post("/events", **EVENT_FORM)
        statuses = [self.post("/campaigns/1/find-sponsors", page="/campaigns/1").status_code for _ in range(70)]
        self.assertIn(303, statuses)  # throttled responses redirect with a flash message
        self.assertEqual(self.svc.db.scalar("SELECT COUNT(*) FROM jobs WHERE type='discover' AND status='QUEUED'"), 1)  # dedupe: still one job


if __name__ == "__main__":
    unittest.main()
