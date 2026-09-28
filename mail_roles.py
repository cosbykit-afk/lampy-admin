"""Mail administrator roles — who can manage James mail.

James has no native admin/moderator roles for mail accounts, so this module
tracks them in the forum PostgreSQL database (table `mail_user_roles`).

Roles:
- admin: full mail administration (manage users, roles, queues)
- moderator: can manage users but not assign admin role
- (no row): regular mail user, no admin access

First-run: if no admin exists, the Mail Admin tab shows a "Create
Administrator" prompt instead of the admin UI.
"""

import db

VALID_ROLES = ("admin", "moderator")


def ensure_table():
    """Create mail_user_roles if it doesn't exist. Idempotent."""
    db.write([(
        """CREATE TABLE IF NOT EXISTS mail_user_roles (
               james_username TEXT PRIMARY KEY,
               role TEXT NOT NULL CHECK (role IN ('admin', 'moderator')),
               created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
               created_by TEXT
           )""",
        (),
    )])


def has_admin():
    """True if at least one mail administrator exists."""
    ensure_table()
    rows = db.query(
        "SELECT 1 FROM mail_user_roles WHERE role = 'admin' LIMIT 1")
    return bool(rows)


def get_role(james_username):
    """Return the role for a James username, or None."""
    ensure_table()
    rows = db.query(
        "SELECT role FROM mail_user_roles WHERE james_username = %s",
        (james_username,))
    return rows[0]["role"] if rows else None


def list_roles():
    """Return all [(james_username, role)] ordered by username."""
    ensure_table()
    rows = db.query(
        "SELECT james_username, role FROM mail_user_roles "
        "ORDER BY james_username")
    return [(r["james_username"], r["role"]) for r in rows]


def set_role(james_username, role, created_by=None):
    """Assign a role. Raises ValueError on bad role."""
    if role not in VALID_ROLES:
        raise ValueError("role must be admin or moderator, got %r" % role)
    ensure_table()
    db.write([(
        """INSERT INTO mail_user_roles (james_username, role, created_by)
           VALUES (%s, %s, %s)
           ON CONFLICT (james_username)
           DO UPDATE SET role = EXCLUDED.role,
                         created_by = EXCLUDED.created_by""",
        (james_username, role, created_by),
    )])


def remove_role(james_username):
    """Remove a user's admin/moderator role (demote to regular user)."""
    ensure_table()
    db.write([(
        "DELETE FROM mail_user_roles WHERE james_username = %s",
        (james_username,),
    )])


def count_admins():
    """Number of users with the admin role."""
    ensure_table()
    rows = db.query(
        "SELECT COUNT(*) AS n FROM mail_user_roles WHERE role = 'admin'")
    return rows[0]["n"]
