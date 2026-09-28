"""W-5 tests: pgai Worker tab (mocked supervisor + DB)."""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("CONSOLE_DEV", "1")

import worker_admin


def test_worker_status_running():
    fake = {"pgai-worker": {"state": "RUNNING",
                            "detail": "pid 123, uptime 1:00:00"}}
    with patch("stack.supervisor_status", return_value=fake):
        ok, info = worker_admin.worker_status()
    assert ok is True and info["state"] == "RUNNING"


def test_worker_status_missing():
    with patch("stack.supervisor_status", return_value={}):
        ok, msg = worker_admin.worker_status()
    assert ok is False


def test_worker_log_tail(tmp_path):
    log = tmp_path / "pgai-worker.log"
    log.write_text("\n".join("line %d" % i for i in range(50)))
    with patch.object(worker_admin, "WORKER_LOG", str(log)):
        ok, lines = worker_admin.worker_log_tail(10)
    assert ok is True and len(lines) == 10 and lines[-1] == "line 49"


def test_worker_log_missing():
    with patch.object(worker_admin, "WORKER_LOG", "/nonexistent/x.log"):
        ok, msg = worker_admin.worker_log_tail()
    assert ok is False


def test_list_vectorizers_empty_when_no_ai_schema():
    import dbbrowser
    with patch.object(dbbrowser, "list_databases",
                      return_value=["postgres", "forum"]), \
         patch.object(dbbrowser, "list_tables",
                      side_effect=Exception("no schema ai")):
        ok, vecs = worker_admin.list_vectorizers()
    assert ok is True and vecs == []


def test_list_vectorizers_found():
    import dbbrowser
    with patch.object(dbbrowser, "list_databases",
                      return_value=["forum"]), \
         patch.object(dbbrowser, "list_tables",
                      return_value=[("vectorizer", "BASE TABLE")]), \
         patch.object(dbbrowser, "run_sql",
                      return_value=(["id"], [[1, "posts", "posts_vec",
                                             "nomic-embed", 768]])):
        ok, vecs = worker_admin.list_vectorizers()
    assert ok is True and len(vecs) == 1
    assert vecs[0]["dimensions"] == 768


def test_job_counts_zeros():
    ok, counts = worker_admin.job_counts()
    assert ok is True and counts["pending"] == 0


# --- route tests ---

import console  # noqa: E402


def _login(c):
    with c.session_transaction() as sess:
        sess["console_user"] = {"id": 7, "username": "musey",
                                "is_admin": True}


def test_route_worker_status():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(worker_admin, "worker_status",
                          return_value=(True, {"state": "RUNNING",
                                              "detail": "pid 1"})), \
             patch.object(worker_admin, "worker_log_tail",
                          return_value=(True, ["a", "b"])):
            r = c.get("/api/worker/status")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"]["state"] == "RUNNING" and d["log"] == ["a", "b"]


def test_route_worker_vectorizers():
    with console.app.test_client() as c:
        _login(c)
        with patch.object(worker_admin, "list_vectorizers",
                          return_value=(True, [])), \
             patch.object(worker_admin, "job_counts",
                          return_value=(True, {"pending": 0})):
            r = c.get("/api/worker/vectorizers")
    assert r.status_code == 200
    assert r.get_json()["vectorizers"] == []


def test_route_worker_anon_redirect():
    with console.app.test_client() as c:
        r = c.get("/worker", follow_redirects=False)
    assert r.status_code in (301, 302, 303)
