"""Security hardening for the Lampy web console.

Copied from the db-console prototype, plus CONSOLE_ALLOW_HTTP:

- Secret key: the app refuses to start without FORUM_SECRET_KEY, unless
  CONSOLE_DEV=1 (local development only) — in which case a random
  per-process key is used and sessions die on restart.
- Session cookies: HttpOnly, SameSite=Lax, and Secure unless CONSOLE_DEV=1
  or CONSOLE_ALLOW_HTTP=1. The Toetop deployment serves plain HTTP on
  localhost:8090, so Secure must be off there or logins silently break
  (the cookie would never be sent back). Set CONSOLE_ALLOW_HTTP=1 only
  for localhost-only deployments.
- CSRF: per-session token, required on every form POST and on JSON
  endpoints (via the X-CSRF-Token header).
- Login rate limiting: per-IP attempt cap with lockout, so password
  guessing is throttled even though accounts live in the forum DB.

The limiter is in-memory: fine for this single-process console. A
multi-worker deployment should replace it with a shared store.
"""

import os
import secrets
import time
from functools import wraps

from flask import jsonify, request, session

DEV_FLAG = "CONSOLE_DEV"


def is_dev():
    return os.environ.get(DEV_FLAG) == "1"


def load_secret_key():
    """Return the session secret, or refuse to start.

    Raises SystemExit when FORUM_SECRET_KEY is unset outside dev mode —
    a hardcoded fallback key would let anyone forge admin sessions.
    """
    key = os.environ.get("FORUM_SECRET_KEY", "")
    if key:
        return key
    if is_dev():
        # Local development only: random key, sessions die with the process.
        return secrets.token_hex(32)
    raise SystemExit(
        "Refusing to start: FORUM_SECRET_KEY is not set. "
        "Set it, or run with CONSOLE_DEV=1 for local development only."
    )


def secure_cookies():
    """False on plain-HTTP localhost deployments."""
    if is_dev():
        return False
    return os.environ.get("CONSOLE_ALLOW_HTTP", "") != "1"


def configure_session_cookies(app):
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,   # no JS access to the session cookie
        SESSION_COOKIE_SAMESITE="Lax",  # top-level navigations only
        SESSION_COOKIE_SECURE=secure_cookies(),
    )


def client_ip():
    # No proxy in front of this console: remote_addr is the real client.
    return request.remote_addr or "unknown"


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

def csrf_token():
    """Per-session CSRF token, creating it on first use."""
    tok = session.get("_csrf_token")
    if not tok:
        tok = secrets.token_hex(32)
        session["_csrf_token"] = tok
    return tok


def rotate_csrf_token():
    session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def _tokens_match(a, b):
    return bool(a) and bool(b) and secrets.compare_digest(str(a), str(b))


def validate_csrf_form():
    """Check the hidden field of a form POST."""
    return _tokens_match(session.get("_csrf_token"),
                         request.form.get("csrf_token"))


def validate_csrf_json():
    """Check the X-CSRF-Token header (or a csrf_token JSON field)."""
    header = request.headers.get("X-CSRF-Token", "")
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:  # noqa: BLE001 — malformed body is not a valid token
        data = {}
    return _tokens_match(session.get("_csrf_token"),
                         header or data.get("csrf_token"))


def csrf_required_json(view):
    """Reject JSON POSTs without a valid CSRF token (403)."""
    @wraps(view)
    def wrapper(*a, **kw):
        if not validate_csrf_json():
            return jsonify({"verdict": "ERROR",
                            "error": "CSRF token missing or invalid"}), 403
        return view(*a, **kw)
    return wrapper


# --------------------------------------------------------------------------
# Login rate limiting
# --------------------------------------------------------------------------

class LoginRateLimiter:
    """Per-IP cap on login attempts with a lockout after too many failures."""

    def __init__(self, max_attempts=5, window_s=300, lockout_s=900):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._hits = {}

    @staticmethod
    def _now():
        return time.monotonic()

    def check(self, ip):
        """None if the attempt may proceed, else seconds until retry."""
        st = self._hits.get(ip)
        if not st:
            return None
        now = self._now()
        if st.get("locked_until", 0) > now:
            return int(st["locked_until"] - now) + 1
        if now - st["first"] > self.window_s:
            del self._hits[ip]  # window expired: fresh start
        return None

    def record_failure(self, ip):
        now = self._now()
        st = self._hits.get(ip)
        if not st or now - st["first"] > self.window_s:
            st = {"count": 0, "first": now, "locked_until": 0}
        st["count"] += 1
        if st["count"] >= self.max_attempts:
            st["locked_until"] = now + self.lockout_s
        self._hits[ip] = st

    def record_success(self, ip):
        self._hits.pop(ip, None)

    def reset(self):
        """Tests only."""
        self._hits.clear()
