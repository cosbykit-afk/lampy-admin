"""HTTPD (Apache) administration — status, config test, vhosts, logs.

Apache runs as the `apache2` supervisord program. This module shells out to
apache2ctl for config tests and vhost/module listings, and reads the log
files directly. All actions are read-only except service control, which
goes through the stack module's whitelisted supervisor_control.
"""

import os
import re
import subprocess

import stack

APACHE_LOG_DIR = "/var/log/apache2"
APACHE_SITES = "/etc/apache2/sites-enabled"


def service_status():
    """(ok, status_dict) for the apache2 program."""
    rc, out = stack._supervisorctl("status", "apache2")
    if rc != 0:
        return False, {"state": "unknown", "detail": out}
    # Parse "apache2 RUNNING pid 123, uptime 1:23:45"
    m = re.match(r"apache2\s+(\w+)(.*)", out.strip())
    state = m.group(1) if m else "unknown"
    detail = m.group(2).strip() if m else out.strip()
    return True, {"state": state, "detail": detail}


def config_test():
    """Run apache2ctl configtest. Returns (ok, output)."""
    try:
        r = subprocess.run(["apache2ctl", "configtest"],
                           capture_output=True, text=True, timeout=15)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and "Syntax OK" in out
        return ok, out.strip()
    except FileNotFoundError:
        return False, "apache2ctl not found"
    except subprocess.TimeoutExpired:
        return False, "configtest timed out"


def list_vhosts():
    """Parse apache2ctl -S output. Returns (ok, [vhost_dict])."""
    try:
        r = subprocess.run(["apache2ctl", "-S"],
                           capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return False, "apache2ctl not found"
    except subprocess.TimeoutExpired:
        return False, "vhost list timed out"
    out = (r.stdout or "") + (r.stderr or "")
    vhosts = []
    # Lines like: "port 80 namevhost example.com (/etc/apache2/sites-enabled/000-default.conf:1)"
    for m in re.finditer(
            r"port (\d+) namevhost (\S+) \((.*):(\d+)\)", out):
        vhosts.append({"port": m.group(1), "servername": m.group(2),
                       "config": m.group(3), "line": m.group(4)})
    # Default vhost lines: "*:80  example.com (/etc/...:1)"
    for m in re.finditer(r"^\S+:\d+\s+(\S+) \((.*):(\d+)\)",
                         out, re.M):
        if not any(v["servername"] == m.group(1) for v in vhosts):
            vhosts.append({"port": "80", "servername": m.group(1),
                           "config": m.group(2), "line": m.group(3)})
    return True, vhosts


def list_modules():
    """apache2ctl -M. Returns (ok, [module_name])."""
    try:
        r = subprocess.run(["apache2ctl", "-M"],
                           capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return False, "apache2ctl not found"
    except subprocess.TimeoutExpired:
        return False, "module list timed out"
    mods = re.findall(r"^\s*(\w+)_module", (r.stdout or ""), re.M)
    return True, sorted(set(mods))


def log_tail(name, lines=100):
    """Tail access or error log. name in {'access', 'error'}."""
    if name not in ("access", "error"):
        return False, "unknown log: %s" % name
    fname = "access.log" if name == "access" else "error.log"
    path = os.path.join(APACHE_LOG_DIR, fname)
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
    except OSError as e:
        return False, str(e)
