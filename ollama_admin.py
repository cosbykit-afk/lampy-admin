#!/usr/bin/env python3
"""Ollama model management backend (W-3).

Thin, honest wrapper over the Ollama REST API (OLLAMA_HOST, default
127.0.0.1:11434). Every function returns (ok, payload); when Ollama is
unreachable the caller gets (False, "Ollama unreachable: ...") — never a
blank page (OLL-08).

Pulls run in a background thread with progress in _PULL_STATE so the UI
can poll; nothing here blocks a request for minutes.
"""

import json
import os
import threading
import urllib.request
import urllib.error

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")

_PULL_STATE = {}  # name -> {"status": str, "done": bool, "error": str|None}
_PULL_LOCK = threading.Lock()


def _api(method, path, data=None, timeout=30):
    """(ok, parsed_json_or_error_text)."""
    url = "http://%s%s" % (OLLAMA_HOST, path)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        return False, "Ollama unreachable at %s: %s" % (OLLAMA_HOST, e)
    except Exception as e:  # noqa: BLE001
        return False, "Ollama request failed: %s: %s" % (
            type(e).__name__, e)


def list_models():
    """(ok, [ {name, size_bytes, modified} ]) — OLL-01."""
    ok, data = _api("GET", "/api/tags")
    if not ok:
        return False, data
    models = []
    for m in data.get("models", []):
        models.append({
            "name": m.get("name", "?"),
            "size_bytes": m.get("size", 0),
            "modified": (m.get("modified_at") or "")[:19].replace("T", " "),
        })
    return True, sorted(models, key=lambda x: x["name"])


def disk_usage():
    """(ok, total_bytes) — sum of installed model sizes (OLL-05)."""
    ok, models = list_models()
    if not ok:
        return False, models
    return True, sum(m["size_bytes"] for m in models)


def show_model(name):
    """(ok, details dict) — /api/show for one model."""
    return _api("POST", "/api/show", {"name": name})


def delete_model(name):
    """(ok, message) — DELETE /api/delete (OLL-04). Caller confirms first."""
    ok, data = _api("DELETE", "/api/delete", {"name": name})
    if not ok:
        return False, data
    return True, "deleted %s" % name


def _pull_worker(name):
    url = "http://%s/api/pull" % OLLAMA_HOST
    req = urllib.request.Request(
        url, data=json.dumps({"name": name}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            for line in resp:
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                status = msg.get("status", "")
                total = msg.get("total") or 0
                completed = msg.get("completed") or 0
                if total:
                    pct = "%d%%" % (100 * completed // total)
                    status = "%s %s" % (status, pct)
                with _PULL_LOCK:
                    _PULL_STATE[name]["status"] = status
    except Exception as e:  # noqa: BLE001
        with _PULL_LOCK:
            _PULL_STATE[name].update(
                {"done": True, "error": "%s: %s" % (type(e).__name__, e)})
        return
    with _PULL_LOCK:
        _PULL_STATE[name].update({"done": True, "status": "complete"})


def pull_model(name):
    """Start a background pull (OLL-03). Returns (ok, message)."""
    name = (name or "").strip()
    if not name:
        return False, "model name is empty"
    with _PULL_LOCK:
        cur = _PULL_STATE.get(name)
        if cur and not cur["done"]:
            return False, "pull of %s already in progress" % name
        _PULL_STATE[name] = {"status": "starting", "done": False,
                             "error": None}
    t = threading.Thread(target=_pull_worker, args=(name,), daemon=True,
                         name="ollama-pull-%s" % name)
    t.start()
    return True, "pull of %s started" % name


def pull_status(name):
    """(ok, state dict) — polled by the UI for progress."""
    with _PULL_LOCK:
        state = _PULL_STATE.get(name)
        if state is None:
            return False, "no pull recorded for %s" % name
        return True, dict(state)


def test_chat(model, prompt):
    """(ok, response_text) — one-shot chat against any model (OLL-06)."""
    ok, data = _api("POST", "/api/chat", {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }, timeout=180)
    if not ok:
        return False, data
    try:
        return True, data["message"]["content"]
    except (KeyError, TypeError):
        return False, "unexpected chat response shape"


def format_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
