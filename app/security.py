"""Auth, CSRF, rate limiting and security headers."""
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import abort, jsonify, redirect, request, session, url_for


class RateLimiter:
    """In-memory sliding window (per process). Put a real limiter (e.g. nginx / Redis) in front for multi-process deploys."""

    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int, window: int) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self):
        with self.lock:
            self.hits.clear()


LIMITS = {  # bucket -> (max requests, window seconds) per client IP
    "login": (10, 300), "send": (30, 60), "expensive": (60, 60), "post": (240, 60),
}


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Invalid or missing CSRF token.")


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="unauthorized"), 401
            return redirect(url_for("web.login", next=request.full_path if request.method == "GET" else None))
        return fn(*a, **k)
    return wrapper


def bucket_for(path: str) -> str:
    if path == "/login":
        return "login"
    if path.endswith("/send") or path.endswith("/bulk"):
        return "send"
    if path.endswith(("/research", "/generate", "/find-sponsors")):
        return "expensive"
    return "post"


def apply_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store" if request.method != "GET" or request.path.startswith(("/leads", "/campaigns", "/api")) else resp.headers.get("Cache-Control", "no-cache")
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                               "script-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    return resp
