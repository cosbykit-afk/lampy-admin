"""Console sign-up — new user registration against the forum users table.

Accounts are stored in the forum PostgreSQL `users` table (same store the
forum app's /register uses and the console's login verifies against).
New accounts get is_admin=false; only the forum's admin flag grants access
to admin-only console areas.

Validation mirrors the forum's registration rules:
- username: 3-20 chars, letters/digits/underscore, unique (case-insensitive)
- email: valid format, unique (case-insensitive)
- password: minimum 8 chars, must match confirmation

Passwords are hashed with werkzeug's generate_password_hash (same as forum).
"""

import re

from werkzeug.security import generate_password_hash

import db

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_signup(username, email, password, confirm):
    """Return (ok, error). error is None when ok."""
    username = (username or "").strip()
    email = (email or "").strip()
    if not USERNAME_RE.match(username):
        return False, "Username must be 3-20 characters: letters, digits, underscore only."
    if not EMAIL_RE.match(email):
        return False, "Enter a valid email address."
    if len(password or "") < 8:
        return False, "Password must be at least 8 characters."
    if password != confirm:
        return False, "Passwords do not match."
    return True, None


def username_taken(username):
    rows = db.query(
        "SELECT 1 FROM users WHERE lower(username) = lower(%s) LIMIT 1",
        (username.strip(),))
    return bool(rows)


def email_taken(email):
    rows = db.query(
        "SELECT 1 FROM users WHERE lower(email) = lower(%s) LIMIT 1",
        (email.strip(),))
    return bool(rows)


def create_user(username, email, password):
    """Insert a new non-admin user. Returns the user dict for the session.

    Raises RuntimeError on DB failure, ValueError on duplicate.
    """
    username = username.strip()
    email = email.strip()
    if username_taken(username):
        raise ValueError("That username is already taken.")
    if email_taken(email):
        raise ValueError("That email is already registered.")
    pw_hash = generate_password_hash(password)
    db.write([(
        "INSERT INTO users (username, email, password_hash, is_admin) "
        "VALUES (%s, %s, %s, false) RETURNING id",
        (username, email, pw_hash),
    )])
    rows = db.query("SELECT id, username, is_admin FROM users WHERE username = %s",
                    (username,))
    row = rows[0]
    return {"id": row["id"], "username": row["username"],
            "is_admin": bool(row["is_admin"])}
