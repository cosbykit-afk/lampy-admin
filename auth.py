"""Console logon module — accounts live in the forum database.

Accounts are created by the forum app itself (/register) and stored in the
forum PostgreSQL `users` table with werkzeug password hashes. This module
only *verifies* credentials against that table and keeps a signed Flask
session. There is no separate account store and no password ever touches
a log or a cookie — only the user id, username, and is_admin flag.

Areas that change data or spend inference budget (the moderation queue)
require an admin account — the forum's `is_admin` flag, which the Musey
account holds.
"""

from functools import wraps

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash

import db


def verify_login(username, password):
    """Return the user row dict on success, None on bad credentials.

    Raises RuntimeError when the database is unreachable, so callers can
    show an honest "logon unavailable" message instead of failing open.
    """
    try:
        rows = db.query(
            "SELECT id, username, password_hash, is_admin FROM users "
            "WHERE username = %s", (username,))
    except Exception as e:  # noqa: BLE001 — no DB, no logon
        raise RuntimeError("account database unreachable: %s" % e)
    if not rows:
        return None
    row = rows[0]
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    return {"id": row["id"], "username": row["username"],
            "is_admin": bool(row["is_admin"])}


def current_console_user():
    u = session.get("console_user")
    return u if isinstance(u, dict) else None


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if current_console_user() is None:
            return redirect(url_for("login", next=request.full_path))
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    """Moderator-only areas. Musey's account is an admin (see seed script)."""
    @wraps(view)
    def wrapper(*a, **kw):
        u = current_console_user()
        if u is None:
            return redirect(url_for("login", next=request.full_path))
        if not u.get("is_admin"):
            return ("Forbidden — moderator accounts only.", 403)
        return view(*a, **kw)
    return wrapper
