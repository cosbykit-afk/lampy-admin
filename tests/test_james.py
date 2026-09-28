"""W-4 tests: James Mail Admin (mocked WebAdmin API)."""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("CONSOLE_DEV", "1")

import james_admin


def test_list_users():
    with patch.object(james_admin, "_request",
                      return_value=(True, ["alice", "bob"])):
        ok, users = james_admin.list_users()
    assert (ok, users) == (True, ["alice", "bob"])


def test_add_user_validates():
    ok, msg = james_admin.add_user("", "pw")
    assert ok is False
    ok, msg = james_admin.add_user("u", "")
    assert ok is False


def test_add_user_calls_api():
    seen = {}

    def fake(method, path, data=None):
        seen.update(method=method, path=path, data=data)
        return True, {}

    with patch.object(james_admin, "_request", fake):
        ok, msg = james_admin.add_user("carol", "s3cret")
    assert ok is True
    assert seen["method"] == "PUT" and seen["path"] == "/users/carol"
    assert seen["data"] == {"password": "s3cret"}


def test_remove_user():
    with patch.object(james_admin, "_request", return_value=(True, {})):
        ok, msg = james_admin.remove_user("bob")
    assert ok is True and "bob" in msg


def test_queue_mails():
    mails = [{"sender": "a@x", "recipients": ["b@x"]}]
    with patch.object(james_admin, "_request", return_value=(True, mails)):
        ok, out = james_admin.queue_mails("spool")
    assert ok is True and out == mails


def test_unreachable_is_clear():
    with patch.object(james_admin, "_request",
                      return_value=(False, "WebAdmin unreachable at x: no")):
        ok, err = james_admin.list_users()
    assert ok is False and "unreachable" in err


# --- route tests ---

import console  # noqa: E402


def _login(c, csrf="csrf-w4"):
    with c.session_transaction() as sess:
        sess["console_user"] = {"id": 7, "username": "musey",
                                "is_admin": True}
        sess["_csrf_token"] = csrf


def test_route_users_list():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(james_admin, "_request",
                          return_value=(True, ["alice"])):
            r = c.get("/api/mailadmin/users")
    assert r.status_code == 200
    assert r.get_json() == {"users": ["alice"]}


def test_route_users_unreachable_502():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(james_admin, "_request",
                          return_value=(False, "WebAdmin unreachable: no")):
            r = c.get("/api/mailadmin/users")
    assert r.status_code == 502


def test_route_add_user_csrf():
    with console.app.test_client() as c:
        _login(c)
        r = c.post("/api/mailadmin/users",
                   json={"username": "x", "password": "y"})
    assert r.status_code == 403


def test_route_add_user_ok():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(james_admin, "_request",
                          return_value=(True, {})):
            r = c.post("/api/mailadmin/users",
                       json={"username": "dave", "password": "pw"},
                       headers={"X-CSRF-Token": "csrf-w4"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_route_remove_user():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(james_admin, "_request",
                          return_value=(True, {})):
            r = c.delete("/api/mailadmin/users/dave",
                         headers={"X-CSRF-Token": "csrf-w4"})
    assert r.status_code == 200


def test_route_anon_redirect():
    with console.app.test_client() as c:
        r = c.get("/mailadmin", follow_redirects=False)
    assert r.status_code in (301, 302, 303)
    assert "/login" in r.headers.get("Location", "")


def test_route_nonadmin_forbidden():
    with console.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["console_user"] = {"id": 8, "username": "pleb",
                                    "is_admin": False}
        r = c.get("/api/mailadmin/users")
    assert r.status_code == 403
