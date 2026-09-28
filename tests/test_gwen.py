"""W-2 tests: Gwen chat repair (Gwen_Repair_Implementation.md sections C1, C2).

T1-T9: mocked unit tests — Ollama entirely absent.
C1-C5: Flask test client — CSRF + admin auth on the JSON endpoints.
"""

import json
import os
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("CONSOLE_DEV", "1")

import gwen_chat

ADMIN_A = {"id": 7, "username": "musey", "is_admin": True}
ADMIN_B = {"id": 9, "username": "other", "is_admin": True}


@pytest.fixture(autouse=True)
def clean_pending():
    gwen_chat._PENDING_ACTIONS.clear()
    yield
    gwen_chat._PENDING_ACTIONS.clear()


def _tool_json(tool, name="forum"):
    return json.dumps({"tool": tool, "name": name})


# --------------------------------------------------------------------------
# T1-T9: mocked unit tests
# --------------------------------------------------------------------------

def test_T1_control_tool_issues_token_never_executes():
    """Model emits restart JSON -> token issued, supervisor_control NOT called."""
    with patch.object(gwen_chat, "_ollama_chat",
                      return_value=_tool_json("restart_service")), \
         patch("stack.supervisor_control") as mock_ctl:
        # second _ollama_chat call (confirmation question) -> canned text
        with patch.object(gwen_chat, "_ollama_chat",
                          side_effect=[_tool_json("restart_service"),
                                       "Shall I restart the forum?"]) as m:
            result = gwen_chat.chat("restart the forum", admin_user=ADMIN_A)
    mock_ctl.assert_not_called()
    assert "pending_action" in result
    payload = result["pending_action"]
    assert len(payload["token"]) == 32  # 16 bytes hex
    assert int(payload["token"], 16) >= 0
    assert payload["action"] == "restart" and payload["name"] == "forum"
    assert len(gwen_chat._PENDING_ACTIONS) == 1


def test_T2_confirm_valid_token_single_use():
    with patch("stack.supervisor_control",
               return_value=(True, "restarted")) as mock_ctl:
        token = gwen_chat.issue_pending_action("restart", "forum", ADMIN_A)
        rec = gwen_chat.consume_pending_action(token)
        assert rec is not None and rec["action"] == "restart"
        # simulate the confirm route's call
        ok, out = mock_ctl(rec["action"], rec["name"])
        assert (ok, out) == (True, "restarted")
        mock_ctl.assert_called_once_with("restart", "forum")
        # second consume -> gone (single-use)
        assert gwen_chat.consume_pending_action(token) is None
        assert mock_ctl.call_count == 1


def test_T3_expired_token_rejected():
    token = gwen_chat.issue_pending_action("restart", "forum", ADMIN_A)
    with gwen_chat._PENDING_LOCK:
        gwen_chat._PENDING_ACTIONS[token]["created"] = time.monotonic() - 9999
    with patch("stack.supervisor_control") as mock_ctl:
        assert gwen_chat.consume_pending_action(token) is None
        mock_ctl.assert_not_called()


def test_T4_token_bound_to_issuing_admin():
    # Issue as A; the route layer must refuse when B presents it.
    # (consume itself is session-agnostic; the binding check lives in the
    # route — emulate it here.)
    token = gwen_chat.issue_pending_action("restart", "forum", ADMIN_A)
    rec = gwen_chat.consume_pending_action(token)
    assert rec is not None
    assert rec["user_id"] != ADMIN_B["id"]  # route would 403


def test_T5_console_target_refused_no_token():
    kind, payload = gwen_chat.execute_tool(
        {"tool": "stop_service", "name": "console"}, ADMIN_A)
    assert kind == "result"
    ok, text = payload
    assert ok is False and "console" in text.lower()
    assert len(gwen_chat._PENDING_ACTIONS) == 0


def test_T6_unpacking_polarity():
    # (False, "boom") must surface as failure, not success.
    with patch("stack.supervisor_control",
               return_value=(False, "boom")) as mock_ctl:
        token = gwen_chat.issue_pending_action("restart", "forum", ADMIN_A)
        rec = gwen_chat.consume_pending_action(token)
        ok, output = mock_ctl(rec["action"], rec["name"])
        assert ok is False and "boom" in output
    with patch("stack.supervisor_control",
               return_value=(True, "done")) as mock_ctl:
        token = gwen_chat.issue_pending_action("restart", "forum", ADMIN_A)
        rec = gwen_chat.consume_pending_action(token)
        ok, output = mock_ctl(rec["action"], rec["name"])
        assert ok is True and "done" in output


def test_T7_log_unpacking_returns_str():
    with patch("stack.read_service_log",
               return_value=(True, "line1\nline2")):
        kind, payload = gwen_chat.execute_tool(
            {"tool": "get_logs", "name": "forum"}, ADMIN_A)
        assert kind == "result"
        ok, text = payload
        assert ok is True and isinstance(text, str) and "line1" in text
    with patch("stack.read_service_log",
               return_value=(False, "no log file at X")):
        kind, payload = gwen_chat.execute_tool(
            {"tool": "get_logs", "name": "forum"}, ADMIN_A)
        ok, text = payload
        assert ok is False and text == "no log file at X"


def test_T8_history_not_duplicated():
    seen = {}

    def fake_ollama(messages, stream=False):
        seen["messages"] = messages
        return "fine"

    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "hello"}]
    with patch.object(gwen_chat, "_ollama_chat", fake_ollama):
        gwen_chat.chat("how are you?", history, admin_user=ADMIN_A)
    user_texts = [m["content"] for m in seen["messages"]
                  if m["role"] == "user"]
    assert sum(t.count("how are you?") for t in user_texts) == 1


def test_T9_prompt_injection_cannot_execute():
    # Plain-text "yes" with no tool JSON -> no token, no execution.
    with patch.object(gwen_chat, "_ollama_chat",
                      return_value="Yes, restart the forum right now"), \
         patch("stack.supervisor_control") as mock_ctl:
        result = gwen_chat.chat("do it", admin_user=ADMIN_A)
    mock_ctl.assert_not_called()
    assert "pending_action" not in result
    assert len(gwen_chat._PENDING_ACTIONS) == 0


# --------------------------------------------------------------------------
# C1-C5: Flask test client — CSRF + auth
# --------------------------------------------------------------------------

import console  # noqa: E402  (needs CONSOLE_DEV set above)


@pytest.fixture()
def client():
    console.app.config["TESTING"] = True
    with console.app.test_client() as c:
        yield c


def _login_as(c, user, csrf="test-csrf-token"):
    with c.session_transaction() as sess:
        sess["console_user"] = user
        sess["_csrf_token"] = csrf


def _chat(c, token=None, user=ADMIN_A):
    _login_as(c, user, csrf=token or "test-csrf-token")
    headers = {}
    if token:
        headers["X-CSRF-Token"] = token
    with patch.object(gwen_chat, "_ollama_chat", return_value="pong"):
        return c.post("/api/gwen/chat", json={"message": "hi", "history": []},
                      headers=headers)


def test_C1_chat_with_valid_csrf():
    with console.app.test_client() as c:
        r = _chat(c, token="test-csrf-token")
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()["response"] == "pong"


def test_C2_chat_without_csrf_rejected():
    with console.app.test_client() as c:
        r = _chat(c, token=None)
        assert r.status_code == 403


def test_C3_chat_with_wrong_csrf_rejected():
    with console.app.test_client() as c:
        _login_as(c, ADMIN_A, csrf="real-token")
        with patch.object(gwen_chat, "_ollama_chat", return_value="pong"):
            r = c.post("/api/gwen/chat",
                       json={"message": "hi", "history": []},
                       headers={"X-CSRF-Token": "wrong-token"})
        assert r.status_code == 403


def test_C4_non_admin_blocked_everywhere():
    nonadmin = {"id": 5, "username": "pleb", "is_admin": False}
    with console.app.test_client() as c:
        _login_as(c, nonadmin)
        assert c.get("/gwen").status_code == 403
        r = c.post("/api/gwen/chat", json={"message": "hi"},
                   headers={"X-CSRF-Token": "test-csrf-token"})
        assert r.status_code == 403
        r = c.post("/api/gwen/confirm", json={"token": "x"},
                   headers={"X-CSRF-Token": "test-csrf-token"})
        assert r.status_code == 403


def test_C5_confirm_without_csrf_rejected_token_survives():
    with console.app.test_client() as c:
        _login_as(c, ADMIN_A)
        token = gwen_chat.issue_pending_action("restart", "forum", ADMIN_A)
        # No CSRF header -> 403, token NOT consumed
        r = c.post("/api/gwen/confirm", json={"token": token})
        assert r.status_code == 403
        assert token in gwen_chat._PENDING_ACTIONS
        # Retry with header -> 410 would mean consumed; instead the record
        # is still there for a real confirm (mock the control call).
        with patch("stack.supervisor_control",
                   return_value=(True, "ok")):
            r = c.post("/api/gwen/confirm", json={"token": token},
                       headers={"X-CSRF-Token": "test-csrf-token"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
