#!/usr/bin/env python3
"""James WebAdmin REST client (W-4).

Server-side only. Talks to the James WebAdmin API on localhost:8001.
Credentials (when JWT auth is enabled per JADM-03) are held in server-side
environment, never in the browser session.

Currently WebAdmin runs without auth (JADM-03 pending); `jwt_token` is None
and requests go direct. When JWT is enabled, set JAMES_JWT_PRIVATE_KEY and
the client signs a token per request.
"""

import json
import os
import urllib.request
import urllib.error

WEBADMIN_BASE = os.environ.get("JAMES_WEBADMIN_URL", "http://localhost:8001")


def _request(method, path, data=None):
    """(ok, parsed_json_or_error)."""
    url = WEBADMIN_BASE + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"})
    # JADM-03: when JWT is enabled, sign and attach a token here.
    # token = _sign_jwt()
    # if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return True, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        return False, "WebAdmin HTTP %d %s %s" % (e.code, path, detail)
    except urllib.error.URLError as e:
        return False, "WebAdmin unreachable at %s: %s" % (WEBADMIN_BASE, e)
    except Exception as e:  # noqa: BLE001
        return False, "WebAdmin request failed: %s: %s" % (
            type(e).__name__, e)


def health():
    """(ok, health_dict)."""
    return _request("GET", "/healthcheck")


def list_users():
    """(ok, [username])."""
    return _request("GET", "/users")


def add_user(username, password):
    """(ok, message). PUT /users/{username} with {"password": ...}."""
    username = (username or "").strip()
    if not username or not password:
        return False, "username and password are required"
    ok, _ = _request("PUT", "/users/" + username, {"password": password})
    if not ok:
        return False, _
    return True, "added user %s" % username


def remove_user(username):
    """(ok, message). DELETE /users/{username}."""
    username = (username or "").strip()
    if not username:
        return False, "username is required"
    ok, err = _request("DELETE", "/users/" + username)
    if not ok:
        return False, err
    return True, "removed user %s" % username


def list_queues():
    """(ok, [queue_name])."""
    return _request("GET", "/mailQueues")


def queue_mails(queue="spool", limit=50):
    """(ok, [mail_summary])."""
    ok, data = _request(
        "GET", "/mailQueues/%s/mails?limit=%d" % (queue, limit))
    if not ok:
        return False, data
    # data is a list of {name, sender, recipients, ...}
    return True, data if isinstance(data, list) else []


def list_mailboxes(username):
    """(ok, [mailbox])."""
    return _request("GET", "/users/%s/mailboxes" % username.strip())
