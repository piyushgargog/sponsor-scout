import base64
import email
import json
import unittest
from email import policy
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app.db import Database
from app.mailer.accounts import AccountStore
from app.mailer.base import MailAuthError, MailError
from app.mailer.gmail import GmailProvider, build_raw_message, new_pkce
from tests.helpers import LogCapture
import shutil
import tempfile

ACCESS, REFRESH = "ya29.FAKE_ACCESS_TOKEN_123456", "1//FAKE_REFRESH_TOKEN_abcdefghijklmnop"


class R:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._d, self.text = code, data or {}, text or json.dumps(data or {})

    def json(self):
        return self._d


class GmailBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(f"{self.tmp}/g.db")
        self.accounts = AccountStore(self.db, "s" * 40)
        self.http = mock.MagicMock()
        self.gm = GmailProvider("cid", "csecret", "http://localhost:5000/gmail/callback", self.accounts, session=self.http)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def connect(self, expires="2999-01-01T00:00:00Z"):
        self.accounts.save("gmail", "me@college.edu", ACCESS, REFRESH, expires, "gmail.send")
        return self.accounts.get_active("gmail")


class OAuthTests(GmailBase):
    def test_authorization_url(self):
        v, ch = new_pkce()
        q = parse_qs(urlparse(self.gm.authorization_url("STATE123", ch)).query)
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["state"], ["STATE123"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertIn("select_account", q["prompt"][0])
        self.assertIn("https://www.googleapis.com/auth/gmail.send", q["scope"][0])
        self.assertNotIn("gmail.readonly", q["scope"][0])
        self.assertNotIn("csecret", self.gm.authorization_url("s", ch))

    def test_exchange_stores_encrypted_tokens(self):
        self.http.post.return_value = R(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
                                              "scope": "openid email https://www.googleapis.com/auth/gmail.send"})
        self.http.get.return_value = R(200, {"email": "Me@College.edu"})
        self.assertEqual(self.gm.complete_oauth("code", "verifier"), "me@college.edu")
        self.assertEqual(self.http.post.call_args[1]["data"]["code_verifier"], "verifier")
        raw = json.dumps(self.db.rows("SELECT * FROM oauth_accounts"))
        self.assertNotIn(ACCESS, raw)
        self.assertNotIn(REFRESH, raw)
        acc = self.accounts.get_active("gmail")
        self.assertEqual((acc["access_token"], acc["refresh_token"]), (ACCESS, REFRESH))

    def test_missing_send_scope_rejected(self):
        self.http.post.return_value = R(200, {"access_token": ACCESS, "scope": "openid email"})
        with self.assertRaises(MailError):
            self.gm.complete_oauth("c", "v")

    def test_token_exchange_error_does_not_leak_secrets(self):
        self.http.post.return_value = R(400, text=f"bad request {REFRESH} client_secret=csecret")
        with self.assertRaises(MailError) as cm:
            self.gm.complete_oauth("c", "v")
        self.assertNotIn(REFRESH, str(cm.exception))
        self.assertNotIn("csecret", str(cm.exception))

    def test_wrong_secret_key_cannot_decrypt(self):
        self.connect()
        other = AccountStore(self.db, "different-secret-key-0123456789abcdef")
        acc = other.get_active("gmail")
        self.assertIsNone(acc["access_token"])
        self.assertIsNone(acc["refresh_token"])


class SendTests(GmailBase):
    def test_mime_message(self):
        raw = build_raw_message("me@college.edu", "them@co.com", "Partnership: Fest × Co", "Hello,\n\nbody ✓", "Prateek")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        self.assertEqual(msg["To"], "them@co.com")
        self.assertIn("me@college.edu", msg["From"])
        self.assertEqual(msg["Subject"], "Partnership: Fest × Co")
        self.assertIn("body ✓", msg.get_content())

    def test_header_injection_impossible(self):
        for subject in ("Hi\r\nBcc: victim@x.com", "Hi\nBcc: victim@x.com"):
            with self.assertRaises(ValueError):
                build_raw_message("me@c.edu", "a@b.com", subject, "x")
        with self.assertRaises(ValueError):
            build_raw_message("me@c.edu", "a@b.com\nBcc: v@x.com", "s", "x")

    def test_send_success_returns_ids(self):
        acc = self.connect()
        self.http.post.return_value = R(200, {"id": "MSG1", "threadId": "THR1"})
        res = self.gm.send(acc, "them@co.com", "S", "B", "Prateek")
        self.assertEqual((res.message_id, res.thread_id), ("MSG1", "THR1"))
        url = self.http.post.call_args[0][0]
        self.assertTrue(url.endswith("/gmail/v1/users/me/messages/send"))
        self.assertEqual(self.http.post.call_args[1]["headers"]["Authorization"], f"Bearer {ACCESS}")
        self.assertIn("raw", self.http.post.call_args[1]["json"])

    def test_expired_token_is_refreshed(self):
        acc = self.connect(expires="2000-01-01T00:00:00Z")
        self.http.post.side_effect = [R(200, {"access_token": "ya29.NEW", "expires_in": 3600}), R(200, {"id": "M", "threadId": "T"})]
        self.gm.send(acc, "a@b.com", "S", "B")
        self.assertEqual(self.http.post.call_args[1]["headers"]["Authorization"], "Bearer ya29.NEW")
        self.assertEqual(self.accounts.get_active("gmail")["access_token"], "ya29.NEW")

    def test_401_triggers_one_refresh_retry(self):
        acc = self.connect()
        self.http.post.side_effect = [R(401, text="expired"), R(200, {"access_token": "ya29.NEW2", "expires_in": 3600}), R(200, {"id": "M2"})]
        self.assertEqual(self.gm.send(acc, "a@b.com", "S", "B").message_id, "M2")

    def test_revoked_grant_deactivates_account(self):
        acc = self.connect(expires="2000-01-01T00:00:00Z")
        self.http.post.return_value = R(400, text='{"error":"invalid_grant"}')
        with self.assertRaises(MailAuthError):
            self.gm.send(acc, "a@b.com", "S", "B")
        self.assertIsNone(self.accounts.get_active("gmail"))

    def test_api_error_does_not_leak_token(self):
        acc = self.connect()
        self.http.post.return_value = R(500, text=f"oops Bearer {ACCESS}")
        with LogCapture() as lc:
            with self.assertRaises(MailError) as cm:
                self.gm.send(acc, "a@b.com", "S", "B")
        self.assertNotIn(ACCESS, str(cm.exception))
        self.assertNotIn(ACCESS, lc.text)

    def test_tokens_not_in_logs_on_connect(self):
        self.http.post.return_value = R(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600, "scope": "https://www.googleapis.com/auth/gmail.send"})
        self.http.get.return_value = R(200, {"email": "me@college.edu"})
        with LogCapture() as lc:
            self.gm.complete_oauth("c", "v")
        self.assertNotIn(ACCESS, lc.text)
        self.assertNotIn(REFRESH, lc.text)
        self.assertIn("gmail_connected", lc.text)


if __name__ == "__main__":
    unittest.main()
