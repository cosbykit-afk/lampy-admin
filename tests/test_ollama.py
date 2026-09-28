"""W-3 tests: Ollama model management (mocked Ollama API)."""

import json
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("CONSOLE_DEV", "1")

import gwen_chat
import ollama_admin

TAGS = {"models": [
    {"name": "gwen:latest", "size": 4_700_000_000,
     "modified_at": "2026-09-20T10:00:00Z"},
    {"name": "llama3.1:8b", "size": 4_900_000_000,
     "modified_at": "2026-09-21T11:00:00Z"},
]}


def test_list_models_parses_tags():
    with patch.object(ollama_admin, "_api", return_value=(True, TAGS)):
        ok, models = ollama_admin.list_models()
    assert ok is True
    assert [m["name"] for m in models] == ["gwen:latest", "llama3.1:8b"]
    assert models[0]["size_bytes"] == 4_700_000_000
    assert models[0]["modified"] == "2026-09-20 10:00:00"


def test_list_models_unreachable_is_not_blank():
    # OLL-08: unreachable Ollama -> (False, clear error), never blank.
    with patch.object(ollama_admin, "_api",
                      return_value=(False, "Ollama unreachable at x: refused")):
        ok, err = ollama_admin.list_models()
    assert ok is False and "unreachable" in err


def test_disk_usage_sums_models():
    with patch.object(ollama_admin, "_api", return_value=(True, TAGS)):
        ok, total = ollama_admin.disk_usage()
    assert ok is True and total == 9_600_000_000
    assert ollama_admin.format_bytes(total).endswith("GB")


def test_delete_model_calls_api():
    seen = {}

    def fake_api(method, path, data=None, timeout=30):
        seen["method"], seen["path"], seen["data"] = method, path, data
        return True, {}

    with patch.object(ollama_admin, "_api", fake_api):
        ok, msg = ollama_admin.delete_model("llama3.1:8b")
    assert ok is True
    assert (seen["method"], seen["path"]) == ("DELETE", "/api/delete")
    assert seen["data"] == {"name": "llama3.1:8b"}


def test_pull_state_machine():
    ollama_admin._PULL_STATE.clear()
    ok, msg = ollama_admin.pull_model("")
    assert ok is False  # empty name refused
    # fake the worker so no network happens
    with patch.object(ollama_admin, "_pull_worker",
                      lambda name: ollama_admin._PULL_STATE[name].update(
                          {"done": True, "status": "complete"})):
        ok, msg = ollama_admin.pull_model("llama3.1:8b")
        assert ok is True
        ok, state = ollama_admin.pull_status("llama3.1:8b")
        assert ok is True and state["done"] is True
    ok, _ = ollama_admin.pull_status("nosuchmodel")
    assert ok is False


def test_test_chat_extracts_content():
    with patch.object(ollama_admin, "_api",
                      return_value=(True, {"message": {"content": "hello"}})):
        ok, text = ollama_admin.test_chat("gwen:latest", "hi")
    assert (ok, text) == (True, "hello")


def test_model_switch_roundtrip(tmp_path):
    override = str(tmp_path / ".gwen_model")
    with patch.object(gwen_chat, "MODEL_OVERRIDE_FILE", override):
        # default: env (GWEN_MODEL unset in test env -> gwen:latest)
        assert gwen_chat.get_model() == os.environ.get(
            "GWEN_MODEL", "gwen:latest")
        ok, msg = gwen_chat.set_model("llama3.1:8b")
        assert ok is True
        assert gwen_chat.get_model() == "llama3.1:8b"
        ok, msg = gwen_chat.set_model("")
        assert ok is False  # empty refused


# --- route tests ---

import console  # noqa: E402


@pytest.fixture()
def client():
    console.app.config["TESTING"] = True
    with console.app.test_client() as c:
        yield c


def _login(c, csrf="csrf-w3"):
    with c.session_transaction() as sess:
        sess["console_user"] = {"id": 7, "username": "musey",
                                "is_admin": True}
        sess["_csrf_token"] = csrf


def test_route_models_unreachable_returns_502_not_blank():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(ollama_admin, "_api",
                          return_value=(False, "Ollama unreachable: no")):
            r = c.get("/api/gwen/models")
    assert r.status_code == 502
    assert "unreachable" in r.get_json()["error"]


def test_route_models_lists():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(ollama_admin, "_api", return_value=(True, TAGS)), \
             patch.object(gwen_chat, "get_model",
                          return_value="gwen:latest"):
            r = c.get("/api/gwen/models")
    assert r.status_code == 200
    d = r.get_json()
    assert len(d["models"]) == 2 and d["gwen_model"] == "gwen:latest"


def test_route_set_model_rejects_unknown():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(ollama_admin, "_api", return_value=(True, TAGS)):
            r = c.post("/api/gwen/model", json={"model": "evil:1"},
                       headers={"X-CSRF-Token": "csrf-w3"})
    assert r.status_code == 400


def test_route_delete_refuses_current_model():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(gwen_chat, "get_model",
                          return_value="gwen:latest"):
            r = c.post("/api/gwen/models/delete",
                       json={"name": "gwen:latest"},
                       headers={"X-CSRF-Token": "csrf-w3"})
    assert r.status_code == 400
    assert "currently using" in r.get_json()["error"]


def test_route_csrf_rejections():
    with console.app.test_client() as c:
        _login(c)
        for path, payload in [
            ("/api/gwen/model", {"model": "x"}),
            ("/api/gwen/models/pull", {"name": "x"}),
            ("/api/gwen/models/delete", {"name": "x"}),
            ("/api/gwen/models/test-chat",
             {"model": "x", "prompt": "y"}),
        ]:
            r = c.post(path, json=payload)  # no CSRF header
            assert r.status_code == 403, path
