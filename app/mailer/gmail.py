"""Gmail via OAuth 2.0 (authorization-code + PKCE). Scope: gmail.send only (+ openid/email to learn the account address).
No passwords are ever stored."""
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlencode

import requests

from ..logging_setup import log_event, redact
from .base import EmailProvider, MailAuthError, MailError, SendResult

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPES = "openid email https://www.googleapis.com/auth/gmail.send"


def new_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_raw_message(sender_email: str, to: str, subject: str, body: str, from_name: str | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = formataddr((from_name or "", sender_email))
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailProvider(EmailProvider):
    name = "gmail"

    def __init__(self, client_id, client_secret, redirect_uri, accounts, session=None):
        self.client_id, self._secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.accounts = accounts
        self.http = session or requests.Session()

    # ---- OAuth -----------------------------------------------------------
    def authorization_url(self, state: str, code_challenge: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "select_account consent", "state": state,
            "code_challenge": code_challenge, "code_challenge_method": "S256", "include_granted_scopes": "true"})

    def complete_oauth(self, code: str, verifier: str) -> str:
        r = self.http.post(TOKEN_URL, timeout=20, data={
            "client_id": self.client_id, "client_secret": self._secret, "code": code, "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": self.redirect_uri})
        if r.status_code != 200:
            raise MailError(f"Token exchange failed (HTTP {r.status_code}): {redact(r.text[:200])}")
        tok = r.json()
        if "https://www.googleapis.com/auth/gmail.send" not in (tok.get("scope") or ""):
            raise MailError("The gmail.send permission was not granted; please reconnect and allow sending.")
        u = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20)
        if u.status_code != 200 or not u.json().get("email"):
            raise MailError("Could not determine the Google account email.")
        email = u.json()["email"].lower()
        exp = (datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.accounts.save("gmail", email, tok["access_token"], tok.get("refresh_token"), exp, tok.get("scope"))
        log_event("gmail_connected", account=email)
        return email

    def _fresh_token(self, account: dict, force=False) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if account.get("access_token") and account.get("expires_at") and account["expires_at"] > now and not force:
            return account["access_token"]
        if not account.get("refresh_token"):
            raise MailAuthError("Gmail authorization is missing or expired; reconnect Gmail.")
        r = self.http.post(TOKEN_URL, timeout=20, data={"client_id": self.client_id, "client_secret": self._secret,
                                                        "refresh_token": account["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code != 200:
            if "invalid_grant" in r.text:
                self.accounts.deactivate(account["id"])
                raise MailAuthError("Gmail access was revoked or expired (invalid_grant). Reconnect Gmail.")
            raise MailError(f"Token refresh failed (HTTP {r.status_code})")
        tok = r.json()
        exp = (datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.accounts.update_access(account["id"], tok["access_token"], exp)
        account["access_token"], account["expires_at"] = tok["access_token"], exp
        return tok["access_token"]

    # ---- sending ---------------------------------------------------------
    def send(self, account, to, subject, body, from_name=None) -> SendResult:
        raw = build_raw_message(account["email"], to, subject, body, from_name)
        for attempt in (1, 2):
            token = self._fresh_token(account, force=attempt == 2)
            try:
                r = self.http.post(SEND_URL, json={"raw": raw}, timeout=30, headers={"Authorization": f"Bearer {token}"})
            except requests.RequestException as e:
                raise MailError(f"Network error while sending: {type(e).__name__}") from e
            if r.status_code == 401 and attempt == 1:
                continue
            if r.status_code == 200:
                j = r.json()
                return SendResult(j.get("id"), j.get("threadId"))
            if r.status_code in (401, 403):
                raise MailAuthError(f"Gmail rejected the request (HTTP {r.status_code}); reconnect Gmail. {redact(r.text[:150])}")
            raise MailError(f"Gmail send failed (HTTP {r.status_code}): {redact(r.text[:200])}")
        raise MailError("Gmail send failed")
