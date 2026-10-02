"""Encrypted OAuth token storage. Tokens are Fernet-encrypted at rest and never logged or sent to the browser."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..util import now_iso


class AccountStore:
    def __init__(self, db, secret_key: str):
        self.db = db
        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("tokens:" + secret_key).encode()).digest()))

    def _enc(self, v):
        return self._f.encrypt(v.encode()).decode() if v else None

    def _dec(self, v):
        if not v:
            return None
        try:
            return self._f.decrypt(v.encode()).decode()
        except InvalidToken:
            return None  # SECRET_KEY changed: user must reconnect

    def save(self, provider, email, access_token=None, refresh_token=None, expires_at=None, scope=None):
        ex = self.db.row("SELECT id, refresh_token_enc FROM oauth_accounts WHERE provider=? AND email=?", (provider, email))
        if ex:
            self.db.run("UPDATE oauth_accounts SET access_token_enc=?, refresh_token_enc=COALESCE(?,refresh_token_enc), expires_at=?, scope=?, active=1 WHERE id=?",
                        (self._enc(access_token), self._enc(refresh_token), expires_at, scope, ex["id"]))
            return ex["id"]
        self.db.run("UPDATE oauth_accounts SET active=0 WHERE provider=?", (provider,))
        return self.db.run("INSERT INTO oauth_accounts(provider,email,access_token_enc,refresh_token_enc,expires_at,scope,created_at) VALUES(?,?,?,?,?,?,?)",
                           (provider, email, self._enc(access_token), self._enc(refresh_token), expires_at, scope, now_iso()))

    def select(self, provider, email):
        self.db.run("UPDATE oauth_accounts SET active=0 WHERE provider=?", (provider,))
        self.db.run("UPDATE oauth_accounts SET active=1 WHERE provider=? AND email=?", (provider, email))

    def get_active(self, provider):
        r = self.db.row("SELECT * FROM oauth_accounts WHERE provider=? AND active=1 ORDER BY id DESC LIMIT 1", (provider,))
        if not r:
            return None
        return {"id": r["id"], "email": r["email"], "access_token": self._dec(r["access_token_enc"]),
                "refresh_token": self._dec(r["refresh_token_enc"]), "expires_at": r["expires_at"], "scope": r["scope"]}

    def update_access(self, account_id, token, expires_at):
        self.db.run("UPDATE oauth_accounts SET access_token_enc=?, expires_at=? WHERE id=?", (self._enc(token), expires_at, account_id))

    def deactivate(self, account_id):
        self.db.run("UPDATE oauth_accounts SET active=0, access_token_enc=NULL, refresh_token_enc=NULL WHERE id=?", (account_id,))

    def list_public(self, provider):
        return self.db.rows("SELECT id, email, active, created_at FROM oauth_accounts WHERE provider=? ORDER BY id DESC", (provider,))
