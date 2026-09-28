"""Service health probes + supervisor control for the Lampy web console.

The console runs INSIDE the `lampy` WSL distro as a supervisord program,
so probes hit distro-localhost (127.0.0.1) and service control goes
through supervisorctl with the distro's real conf
(/etc/supervisor/conf.d/lampy.conf). No Docker API is involved.

- probes: real TCP/HTTP/banner checks — a failed probe reports red with
  the error, never green. James must answer a 220 SMTP greeting.
- pgai-worker: REAL liveness from `supervisorctl status` (RUNNING=green).
- control: restart/stop/start per program, gated to admin routes in
  console.py. Logs are tailed straight from /var/log/supervisor/.

STACK_SERVICES are the seven stack programs. `bible` and `console` are
also managed by the same supervisord but are not stack services; the
console is deliberately excluded from stop-all/restart-all so the UI
never kills itself mid-request.
"""

import http.client
import json
import os
import re
import socket
import subprocess

TARGET = os.environ.get("CONSOLE_TARGET_HOST", "127.0.0.1")
TIMEOUT = float(os.environ.get("CONSOLE_PROBE_TIMEOUT", "5"))
SUPERVISOR_CONF = os.environ.get("CONSOLE_SUPERVISOR_CONF",
                                 "/etc/supervisor/conf.d/lampy.conf")
LOG_DIR = os.environ.get("CONSOLE_LOG_DIR", "/var/log/supervisor")

STACK_SERVICES = ["postgres", "apache2", "forum", "ollama", "james",
                  "pgai-worker", "codeserver"]
MANAGED = STACK_SERVICES + ["bible", "console"]


# --------------------------------------------------------------------------
# supervisorctl
# --------------------------------------------------------------------------

def _supervisorctl(*args):
    """Run supervisorctl with the distro conf. Returns (rc, output)."""
    try:
        p = subprocess.run(
            ["supervisorctl", "-c", SUPERVISOR_CONF] + list(args),
            capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "supervisorctl not found"
    except subprocess.TimeoutExpired:
        return 124, "supervisorctl timed out"


def supervisor_status():
    """{name: {"state":..., "detail":...}} — never raises.

    NOTE: `supervisorctl status` exits NON-ZERO (rc=3) when any program
    is not RUNNING (BACKOFF/FATAL/...), while still printing the full
    table on stdout. So the table is parsed whenever it yields program
    lines, regardless of rc; {"_error": ...} is only returned when
    nothing parseable came back.
    """
    _STATES = ("RUNNING", "STOPPED", "STARTING", "BACKOFF", "STOPPING",
               "EXITED", "FATAL", "UNKNOWN")
    rc, out = _supervisorctl("status")
    states = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+(%s)\s*(.*)$" % "|".join(_STATES),
                     line.strip())
        if m:
            states[m.group(1)] = {"state": m.group(2),
                                  "detail": m.group(3).strip()}
    if states:
        return states
    return {"_error": out or "supervisorctl status failed (rc=%d)" % rc}


def supervisor_control(action, name):
    """(ok, message). action in {start, stop, restart}; name whitelisted
    to MANAGED so the route layer cannot reach arbitrary programs."""
    if action not in ("start", "stop", "restart"):
        return False, "unknown action: %s" % action
    if name not in MANAGED:
        return False, "unknown program: %s" % name
    rc, out = _supervisorctl(action, name)
    ok = rc == 0 and "ERROR" not in out.upper()
    return ok, out or ("%s %s: no output" % (action, name))


def read_service_log(name, lines=200):
    """Tail of /var/log/supervisor/<name>.log. Returns (ok, text)."""
    if name not in MANAGED:
        return False, "unknown program: %s" % name
    path = os.path.join(LOG_DIR, name + ".log")
    if not os.path.isfile(path):
        return False, "no log file at %s" % path
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            block = 65536
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        text = data.decode("utf-8", "replace")
        return True, "\n".join(text.splitlines()[-lines:])
    except Exception as e:  # noqa: BLE001 — surfaced to the UI
        return False, "%s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

def _tcp_probe(port):
    """(ok, detail) — real TCP connect."""
    s = socket.socket()
    s.settimeout(TIMEOUT)
    try:
        s.connect((TARGET, port))
        return True, "tcp connect ok"
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        s.close()


def _http_probe(port, path, parse_json=False):
    """(ok, detail) — real HTTP GET; any 2xx/3xx counts as alive."""
    try:
        conn = http.client.HTTPConnection(TARGET, port, timeout=TIMEOUT)
        conn.request("GET", path, headers={"Host": TARGET})
        resp = conn.getresponse()
        body = resp.read(4096)
        ok = 200 <= resp.status < 400
        detail = "HTTP %d" % resp.status
        if ok and parse_json:
            try:
                data = json.loads(body.decode("utf-8", "replace"))
                if isinstance(data, dict) and "models" in data:
                    detail += ", %d model(s)" % len(data["models"])
            except Exception:  # noqa: BLE001 — body isn't JSON; ignore
                pass
        conn.close()
        return ok, detail
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def _banner_probe(port):
    """(ok, banner) — TCP connect and read the service banner line."""
    s = socket.socket()
    s.settimeout(TIMEOUT)
    try:
        s.connect((TARGET, port))
        try:
            banner = s.recv(120).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            banner = ""
        return True, banner
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        s.close()


def _card(name, label, ok, detail, port):
    return {"name": name, "label": label, "ok": ok,
            "detail": detail, "port": port}


def probe_postgres():
    ok, detail = _tcp_probe(5432)
    if ok:
        import db
        qok, qerr = db.db_ping()
        detail += ("; SQL query ok" if qok else "; SQL query FAILED: %s" % qerr)
        ok = qok
    return _card("postgres",
                 "PostgreSQL 16 + TimescaleDB + pgvector + pgAI",
                 ok, detail, 5432)


def probe_apache():
    ok, detail = _http_probe(80, "/")
    return _card("apache2", "Apache HTTPD (web / TLS)", ok, detail, 80)


def probe_forum():
    ok, detail = _http_probe(80, "/app/")
    return _card("forum", "Forum app (gunicorn/Flask via /app/)",
                 ok, detail, 80)


def probe_ollama():
    ok, detail = _http_probe(11434, "/api/tags", parse_json=True)
    return _card("ollama", "Ollama (embeddings)", ok, detail, 11434)


def probe_james():
    ok, banner = _banner_probe(2587)
    if ok and not banner.startswith("220"):
        ok = False
        detail = "no 220 SMTP greeting (got: %s)" % (banner or "<empty>")
    elif ok:
        detail = "banner: %s" % banner
    else:
        detail = banner  # the error text
    return _card("james", "James (mail)", ok, detail, 2587)


def probe_pgai_worker():
    states = supervisor_status()
    info = states.get("pgai-worker")
    if info is None:
        return _card("pgai-worker", "pgAI vectorizer worker", False,
                     "supervisor state unknown: %s"
                     % states.get("_error", "no data"), None)
    ok = info["state"] == "RUNNING"
    detail = "supervisor: %s %s" % (info["state"], info["detail"])
    return _card("pgai-worker", "pgAI vectorizer worker", ok,
                 detail.strip(), None)


def probe_codeserver():
    ok, detail = _http_probe(8080, "/")
    return _card("codeserver", "code-server (workspace IDE)",
                 ok, detail, 8080)


PROBES = [probe_postgres, probe_apache, probe_forum, probe_ollama,
          probe_james, probe_pgai_worker, probe_codeserver]


def probe_all():
    """Run every probe; combine with supervisor state. Returns list of
    card dicts. Never raises.

    A card is green ONLY when BOTH hold:
      1. the native supervisor program is RUNNING, and
      2. its functional probe succeeds.
    If a forwarded/container endpoint answers while the native program
    is not running, the card is forced red and says so explicitly.
    """
    states = supervisor_status()
    cards = []
    for fn in PROBES:
        try:
            card = fn()
        except Exception as e:  # noqa: BLE001 — a probe must never 500
            card = _card(fn.__name__, fn.__name__, False,
                         "probe crashed: %s: %s"
                         % (type(e).__name__, e), None)
        probe_ok = card["ok"]
        st = states.get(card["name"])
        state = st["state"] if st else "unknown"
        card["svc_state"] = state
        card["svc_detail"] = st["detail"] if st else states.get("_error", "")
        if state != "RUNNING" and probe_ok:
            # Endpoint answered (possibly forwarded/container) but the
            # native program is not RUNNING: force red, never fake green.
            card["ok"] = False
            card["detail"] = (
                "ENDPOINT ANSWERED but native supervisor program is %s "
                "(not RUNNING) — card forced red. %s"
                % (state, card["detail"]))
        cards.append(card)
    return cards
