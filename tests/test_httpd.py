"""W-9 HTTPD tests (mocked subprocess/supervisor)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch, MagicMock
import httpd_admin


def _run_result(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


def test_service_status_running():
    with patch.object(httpd_admin.stack, "_supervisorctl",
                      return_value=(0, "apache2 RUNNING pid 123, uptime 1:00:00")):
        ok, s = httpd_admin.service_status()
    assert ok and s["state"] == "RUNNING"


def test_service_status_stopped():
    with patch.object(httpd_admin.stack, "_supervisorctl",
                      return_value=(0, "apache2 STOPPED")):
        ok, s = httpd_admin.service_status()
    assert ok and s["state"] == "STOPPED"


def test_config_test_ok():
    with patch("subprocess.run",
               return_value=_run_result("Syntax OK\n")):
        ok, out = httpd_admin.config_test()
    assert ok and "Syntax OK" in out


def test_config_test_fail():
    with patch("subprocess.run",
               return_value=_run_result("Syntax error", returncode=1)):
        ok, out = httpd_admin.config_test()
    assert not ok


def test_list_vhosts():
    sample = ("VirtualHost configuration:\n"
              "*:80  example.com (/etc/apache2/sites-enabled/000-default.conf:1)\n"
              "port 80 namevhost forum.local (/etc/apache2/sites-enabled/forum.conf:2)\n")
    with patch("subprocess.run", return_value=_run_result(sample)):
        ok, vhosts = httpd_admin.list_vhosts()
    assert ok and len(vhosts) >= 1
    assert any(v["servername"] == "forum.local" for v in vhosts)


def test_list_modules():
    sample = "Loaded Modules:\n core_module (static)\n rewrite_module (shared)\n"
    with patch("subprocess.run", return_value=_run_result(sample)):
        ok, mods = httpd_admin.list_modules()
    assert ok and "rewrite" in mods and "core" in mods


def test_log_tail_bad_name():
    ok, msg = httpd_admin.log_tail("bogus")
    assert not ok


def test_log_tail_missing_file():
    with patch("os.path.isfile", return_value=False):
        ok, msg = httpd_admin.log_tail("access")
    assert not ok and "no log file" in msg
