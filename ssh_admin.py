"""Embedded SSH admin server for the Lampy web console.

Listens on CONSOLE_SSH_BIND:CONSOLE_SSH_PORT (default 127.0.0.1:8022).
Key-based auth ONLY — no passwords, no shell, no SFTP, no arbitrary
commands. The authorized keys are Kit's existing Windows SSH
authorized_keys (C:\\ProgramData\\ssh\\administrators_authorized_keys,
visible in the distro at
/mnt/c/ProgramData/ssh/administrators_authorized_keys), read live at
each login so key changes take effect immediately (override the path
with CONSOLE_SSH_AUTH_KEYS, e.g. for testing).

Exec-only restricted vocabulary (see HELP_TEXT): service status /
start / stop / restart, log tails, forum admin (users, thread lock,
DB backup stream), metrics — every admin operation the web UI offers.
Anything outside the vocabulary is rejected with exit code 2.

Runs in a thread inside the console process (own asyncio loop); the
web UI is untouched. Started from console.py's __main__.

REACHABILITY NOTE (2026-09-27, verified): the lampy WSL distro
currently has no working localhost forwarding to Windows — neither
127.0.0.1-bound nor 0.0.0.0-bound distro ports are reachable from
Windows, and the distro's eth0 IP is not routable from Windows
either (tested 8090, 8000, and a fresh 18099 probe). So 8022 — like
the web UI's 8090 — is live in the distro but not yet reachable from
Windows. Both light up with zero code changes once the WSL ingress
is repaired (Kit's call: mirrored networking mode, or a relay reset
via wsl --shutdown after the image validation finishes).
"""

import asyncio
import os
import shlex
import subprocess

import asyncssh

SSH_BIND = os.environ.get("CONSOLE_SSH_BIND", "127.0.0.1")
SSH_PORT = int(os.environ.get("CONSOLE_SSH_PORT", "8022"))

_AUTH_KEYS_DEFAULT = \
    "/mnt/c/ProgramData/ssh/administrators_authorized_keys"
_HOST_KEY_DEFAULT = "/opt/lampy-console/ssh_host_key"

HELP_TEXT = """\
Lampy console SSH admin — restricted commands (exec only, no shell):

  help                          this list
  status                        the seven service probes + supervisor state
  restart <svc>                 supervisorctl restart <svc>
  start <svc>                   supervisorctl start <svc>
  stop <svc>                    supervisorctl stop <svc>
  restart-all | start-all | stop-all
                                act on the seven stack services
                                (the console itself is excluded)
  logs <svc> [lines]            tail /var/log/supervisor/<svc>.log (default 200)
  users                         forum users: id, username, email, admin
  threads [limit]               recent threads: id, locked, category, author, title
  lock <thread_id>              lock a thread (admin forum op)
  unlock <thread_id>            unlock a thread
  backup                        stream a pg_dump of the forum DB to stdout
  metrics [days]                forum events per day (default 14)

<svc> is one of: postgres apache2 forum ollama james pgai-worker codeserver
bible console. Key auth only; the key is the identity (username ignored).
"""


def _auth_keys_path():
    return os.environ.get("CONSOLE_SSH_AUTH_KEYS", _AUTH_KEYS_DEFAULT)


def _host_key_path():
    return os.environ.get("CONSOLE_SSH_HOST_KEY", _HOST_KEY_DEFAULT)


def _key_allowed(offered):
    """True when the offered public key is in Kit's authorized_keys."""
    try:
        lines = open(_auth_keys_path(), "r",
                     encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return False

    want = offered.export_public_key()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if asyncssh.import_public_key(line).export_public_key() == want:
                return True
        except Exception:  # noqa: BLE001 — skip malformed lines
            continue
    return False


def _ensure_host_key():

    path = _host_key_path()
    if not os.path.exists(path):
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(path)
        os.chmod(path, 0o600)
    return path


class _Server(asyncssh.SSHServer):
    """Auth policy for asyncssh: public-key only, no passwords."""

    def public_key_auth_supported(self):
        # Default is False — without this override the server offers no
        # auth methods at all and every login is denied.
        return True

    def password_auth_supported(self):
        return False

    def kbdint_auth_supported(self):
        return False

    def validate_public_key(self, username, key):
        # The key is the identity; the username is ignored.
        return _key_allowed(key)


# --------------------------------------------------------------------------
# Command implementations (all async; blocking work goes to the executor)
# --------------------------------------------------------------------------

async def _run_blocking(loop, fn, *args):
    return await loop.run_in_executor(None, fn, *args)


async def _cmd_help(process, args, loop):
    process.stdout.write(HELP_TEXT)
    return 0


async def _cmd_status(process, args, loop):
    import stack as stackmod
    cards = await _run_blocking(loop, stackmod.probe_all)
    lines = ["%-4s %-12s %-9s %s" % ("PROBE", "SERVICE", "SUPERVISOR",
                                    "DETAIL")]
    for c in cards:
        mark = "OK" if c["ok"] else "FAIL"
        lines.append("%-4s %-12s %-9s %s"
                     % (mark, c["name"], c["svc_state"],
                        (c["detail"] or "")[:100]))
    process.stdout.write("\n".join(lines) + "\n")
    return 0


def _svc_arg(args):
    import stack as stackmod
    if not args:
        return None, "usage: <cmd> <svc>"
    if args[0] not in stackmod.MANAGED:
        return None, ("unknown service '%s' (see `help`)" % args[0])
    return args[0], None


async def _cmd_control(process, args, loop, action):
    import stack as stackmod
    name, err = _svc_arg(args)
    if err:
        process.stderr.write(err + "\n")
        return 2
    if name == "console" and action in ("restart", "stop"):
        process.stdout.write(
            "warning: this will drop your SSH session; "
            "supervisord will restart the console.\n")
    ok, msg = await _run_blocking(
        loop, stackmod.supervisor_control, action, name)
    process.stdout.write(msg + "\n")
    return 0 if ok else 1


async def _cmd_control_all(process, args, loop, action):
    import stack as stackmod
    rc = 0
    for name in stackmod.STACK_SERVICES:  # console excluded, like the web UI
        ok, msg = await _run_blocking(
            loop, stackmod.supervisor_control, action, name)
        process.stdout.write("%-12s %s: %s\n"
                             % (name, "ok" if ok else "FAILED", msg))
        if not ok:
            rc = 1
    return rc


async def _cmd_logs(process, args, loop):
    import stack as stackmod
    name, err = _svc_arg(args)
    if err:
        process.stderr.write(err + "\n")
        return 2
    try:
        lines = max(10, min(5000, int(args[1]))) if len(args) > 1 else 200
    except ValueError:
        process.stderr.write("usage: logs <svc> [lines]\n")
        return 2
    ok, text = await _run_blocking(loop, stackmod.read_service_log,
                                   name, lines)
    if not ok:
        process.stderr.write(text + "\n")
        return 1
    process.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    return 0


async def _cmd_users(process, args, loop):
    import db
    try:
        users = await _run_blocking(loop, db.get_users)
    except Exception as e:  # noqa: BLE001
        process.stderr.write("database error: %s: %s\n"
                             % (type(e).__name__, e))
        return 1
    lines = ["%-4s %-20s %-30s %-5s %s"
             % ("ID", "USERNAME", "EMAIL", "ADMIN", "CREATED")]
    for u in users:
        lines.append("%-4s %-20s %-30s %-5s %s"
                     % (u["id"], u["username"], u["email"] or "",
                        "Y" if u["is_admin"] else "N", u["created_at"]))
    process.stdout.write("\n".join(lines) + "\n")
    return 0


async def _cmd_threads(process, args, loop):
    import db
    try:
        limit = max(1, min(200, int(args[0]))) if args else 20
    except ValueError:
        process.stderr.write("usage: threads [limit]\n")
        return 2
    try:
        threads = await _run_blocking(loop, db.get_threads_for_admin, limit)
    except Exception as e:  # noqa: BLE001
        process.stderr.write("database error: %s: %s\n"
                             % (type(e).__name__, e))
        return 1
    lines = ["%-5s %-6s %-15s %-15s %-5s %s"
             % ("ID", "LOCKED", "CATEGORY", "AUTHOR", "POSTS", "TITLE")]
    for t in threads:
        lines.append("%-5s %-6s %-15s %-15s %-5s %s"
                     % (t["id"], "Y" if t["is_locked"] else "N",
                        (t["cat_name"] or "")[:15],
                        (t["author"] or "")[:15], t["post_count"],
                        (t["title"] or "")[:70]))
    process.stdout.write("\n".join(lines) + "\n")
    return 0


async def _cmd_lock(process, args, loop, locked):
    import db
    if not args:
        process.stderr.write("usage: lock|unlock <thread_id>\n")
        return 2
    try:
        tid = int(args[0])
    except ValueError:
        process.stderr.write("thread_id must be an integer\n")
        return 2
    try:
        await _run_blocking(loop, db.set_thread_locked, tid, locked)
    except Exception as e:  # noqa: BLE001
        process.stderr.write("database error: %s: %s\n"
                             % (type(e).__name__, e))
        return 1
    process.stdout.write("thread %d %s\n"
                         % (tid, "locked" if locked else "unlocked"))
    return 0


async def _cmd_backup(process, args, loop):
    import codecs
    import db
    cmd = ["pg_dump", "-h", db.DB_HOST, "-p", str(db.DB_PORT),
           "-U", db.DB_USER, "-d", db.DB_NAME]
    env = dict(os.environ)
    env["PGPASSWORD"] = db.DB_PASS
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
    except FileNotFoundError:
        process.stderr.write("pg_dump not found\n")
        return 1
    # The SSH channel is text-mode: decode the dump as UTF-8 while
    # streaming (the forum DB is UTF8; pg_dump text output is UTF-8).
    # Abort rather than silently corrupt on undecodable bytes.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        try:
            text = decoder.decode(chunk)
        except UnicodeDecodeError as e:
            proc.kill()
            process.stderr.write(
                "backup aborted: non-UTF8 bytes in dump: %s\n" % e)
            return 1
        process.stdout.write(text)
        await asyncio.sleep(0)  # yield so the session stays alive
    tail = decoder.decode(b"", final=True)
    if tail:
        process.stdout.write(tail)
    err = (await proc.stderr.read()).decode("utf-8", "replace")
    rc = await proc.wait()
    if rc != 0:
        process.stderr.write("pg_dump failed (rc=%d): %s\n"
                             % (rc, err[:500]))
    return rc


async def _cmd_metrics(process, args, loop):
    import db
    try:
        days_n = max(1, min(365, int(args[0]))) if args else 14
    except ValueError:
        process.stderr.write("usage: metrics [days]\n")
        return 2
    try:
        rows = await _run_blocking(loop, db.get_metrics, 5000)
    except Exception as e:  # noqa: BLE001
        process.stderr.write("database error: %s: %s\n"
                             % (type(e).__name__, e))
        return 1
    days = {}
    for r in rows:
        days.setdefault(str(r["day"]), {})[r["event_type"]] = r["n"]
    ordered = sorted(days.items(), reverse=True)[:days_n]
    lines = ["%-12s %s" % ("DAY", "EVENTS")]
    for day, evs in ordered:
        lines.append("%-12s %s" % (
            day, ", ".join("%s=%s" % kv for kv in sorted(evs.items()))))
    process.stdout.write("\n".join(lines) + "\n")
    return 0


_COMMANDS = {
    "help": _cmd_help,
    "status": _cmd_status,
    "restart": lambda p, a, l: _cmd_control(p, a, l, "restart"),
    "start": lambda p, a, l: _cmd_control(p, a, l, "start"),
    "stop": lambda p, a, l: _cmd_control(p, a, l, "stop"),
    "restart-all": lambda p, a, l: _cmd_control_all(p, a, l, "restart"),
    "start-all": lambda p, a, l: _cmd_control_all(p, a, l, "start"),
    "stop-all": lambda p, a, l: _cmd_control_all(p, a, l, "stop"),
    "logs": _cmd_logs,
    "users": _cmd_users,
    "threads": _cmd_threads,
    "lock": lambda p, a, l: _cmd_lock(p, a, l, True),
    "unlock": lambda p, a, l: _cmd_lock(p, a, l, False),
    "backup": _cmd_backup,
    "metrics": _cmd_metrics,
}


# One admin command at a time (see _handle_process). Module-level;
# binds to the SSH server's asyncio loop on first use (one thread).
_command_lock = asyncio.Lock()


async def _handle_process(process):
    cmdline = process.command  # exec command string, or None for shell
    if not cmdline:
        process.stderr.write(
            "interactive shell not supported — use exec, e.g.\n"
            '  ssh -p %d console@host "status"\n(see `help` for commands)\n'
            % SSH_PORT)
        process.exit(1)
        return
    try:
        argv = shlex.split(cmdline)
    except ValueError as e:
        process.stderr.write("could not parse command: %s\n" % e)
        process.exit(2)
        return
    if not argv:
        process.exit(0)
        return
    handler = _COMMANDS.get(argv[0])
    if handler is None:
        process.stderr.write("unknown command '%s' (see `help`)\n" % argv[0])
        process.exit(2)
        return
    loop = asyncio.get_running_loop()

    async def _run():
        try:
            rc = await handler(process, argv[1:], loop)
        except Exception as e:  # noqa: BLE001
            process.stderr.write("error: %s: %s\n" % (type(e).__name__, e))
            rc = 1
        process.exit(rc if isinstance(rc, int) else 0)

    # Busy signal: one admin command at a time. If another SSH exec is
    # already running, reject gracefully (exit 3) instead of running
    # concurrently and risking supervisor/DB conflicts. `help` is exempt
    # so it stays available.
    if argv[0] == "help":
        await _run()
    elif _command_lock.locked():
        process.stderr.write(
            "busy: another admin command is already running; "
            "try again in a moment\n")
        process.exit(3)
    else:
        async with _command_lock:
            await _run()


def start_ssh_server():
    """Blocking: serve the SSH admin port forever. Runs in a thread."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if not os.path.exists(_auth_keys_path()):
        print("ssh-admin: WARNING: authorized_keys not found at %s — "
              "all SSH logins will fail" % _auth_keys_path(), flush=True)

    async def _main():
        # NOTE (asyncssh 2.24.0, verified by live test): server_factory and
        # server_host_keys go through an explicit SSHServerConnectionOptions
        # object, while process_factory is passed as a direct listen()
        # kwarg. Other combinations silently break: the factory is never
        # called (logins denied) or exec requests never reach the handler
        # (session closes immediately).
        opts = asyncssh.SSHServerConnectionOptions(
            server_factory=_Server,
            server_host_keys=[_ensure_host_key()],
        )
        await asyncssh.listen(
            SSH_BIND, SSH_PORT,
            options=opts,
            process_factory=_handle_process,
        )
        print("ssh-admin: listening on %s:%d" % (SSH_BIND, SSH_PORT),
              flush=True)
        await asyncio.Future()  # serve forever

    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()
