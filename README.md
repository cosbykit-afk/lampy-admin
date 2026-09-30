# Lampy Administration App

Web + SSH administration console for the Lampy forum stack. Runs **inside**
the `lampy` WSL distro on Toetop as a supervisord program (`[program:console]`,
root), binds distro-localhost `127.0.0.1:8090` (web) and `127.0.0.1:8022`
(SSH admin). No Docker involved.

## What it is

One honest dashboard for all nine supervisor-managed services
(postgres, apache2, forum, ollama, james, pgai-worker, codeserver, bible,
console itself): live health cards (a card is green only when the native
program is RUNNING **and** its functional probe succeeds), log viewer,
per-service restart/stop/start for admins, forum browsing, database
browser, mail, and an SSH admin server with the same powers for
terminal users.

## Module map

| Module | Scope | Status |
|---|---|---|
| Base console | Stack/Forum/Write/Search/Docs/Metrics/Mail/Admin tabs, SSH admin on 8022 | Implemented |
| W-1 | Generic PostgreSQL database browser tab (read-only by default, per-session write confirm, 30s statement timeout, CSV export) | Implemented, unit-tested |
| W-0 | James WebAdmin stabilization (port 8001, auth) | Pending |
| W-2 | Gwen chat tab (repaired: pending-action tokens, separate confirm endpoint, audit log) | Implemented, mocked-tested (live tests need gwen:latest) |
| W-3 | Ollama model management (list/pull/delete/test-chat/switch, disk usage) | Implemented, mocked-tested |
| W-4 | Mail Admin tab (James users, queues, mailboxes via WebAdmin) | Implemented, mocked-tested, live CRUD verified |
| W-5 | pgai Worker tab (process status, vectorizer catalog, VEC-07 empty state) | Implemented, mocked-tested |
| W-6 | Full keyboard accessibility audit (static audit + focus styles + label fixes) | Complete |
| W-8 | User signup (registration against forum users table, werkzeug hashes) | Implemented, tested (9 passing), live |
| W-9 | HTTPD tab (Apache status, config test, vhosts, modules, logs, restart) | Implemented, tested (8 passing), live |
| W-7 | Final verification against acceptance criteria | Pending |

Each module is implemented, tested, and debugged on its own before the
next begins. See `docs/HOW_TO_RUN.md` for the operator's guide.

## Running it

Production (inside the `lampy` distro):

```
cp deploy/lampy-console.supervisor.conf /etc/supervisor/conf.d/   # fill in secrets first
supervisorctl reread && supervisorctl update
```

The app reads all configuration from the environment (see
`deploy/lampy-console.supervisor.conf` for the full list). It refuses to
start without `FORUM_SECRET_KEY` outside dev mode — a hardcoded fallback
would let anyone forge admin sessions.

Development:

```
pip install -r requirements.txt
CONSOLE_ALLOW_HTTP=1 FORUM_SECRET_KEY=dev-only \
  FORUM_DB_HOST=127.0.0.1 FORUM_DB_PORT=5432 FORUM_DB_NAME=forum \
  FORUM_DB_USER=forum FORUM_DB_PASS=... \
  python console.py
```

Then open http://127.0.0.1:8090. Log in with a forum account; the Admin
tab and service-control buttons require the forum's admin flag.

## Security model

- Binds `127.0.0.1` only — never exposed to the LAN.
- All credentials arrive via the supervisor environment (root-only,
  mode 600 conf file). None are hardcoded, logged, or rendered.
- Database tab: read-only by default; write mode is per session and per
  database with an explicit warning confirmation; every statement gets a
  30-second timeout; CSV export is always read-only.
- CSRF tokens on all state-changing forms; admin gates on all
  privileged routes.
- SSH admin (8022): key auth only against the existing Windows
  authorized_keys, no password, no interactive shell, single-command
  invocations. The console can never stop or restart itself.

## Tests

```
python -m pytest tests/ -q
```

## Layout

```
console.py      Flask app, routes, service probes
dbbrowser.py    W-1 database browser backend (psycopg, allowlisted)
auth.py         forum-account login, admin gates
db.py           forum database access
mail.py         James mailbox access
security.py     CSRF, secret-key loading, headers
ssh_admin.py    embedded asyncssh server (port 8022)
stack.py        supervisor service control
templates/      Jinja templates (all values escaped)
static/         CSS
tests/          pytest suite
docs/           operator + design docs
deploy/         supervisor program snippet (fill in secrets on the box)
```

## Build status (2026-09-30)

- **Release `v1.1.0`** (2026-09-28) — Mail module (James WebAdmin user
  management, IMAP/SMTP with STARTTLS), R Theory module (site parser,
  pgvector semantic search), Windows Tools (port forwarding status).
- **Deployed** — runs inside the `lampy` WSL distro on Toetop as the
  supervisord `[program:console]` (root), web on `127.0.0.1:8090`,
  SSH admin on `127.0.0.1:8022`.
- **Supervisord incident resolved** (2026-09-30) — duplicate-supervisord
  race fixed with a flock guard; connector pools raised; all 12 distro
  programs verified RUNNING.

## Known issues

- **WSL localhost forwarding is broken on the reference host** — services
  bound to distro `127.0.0.1` are unreachable from Windows PowerShell
  (verified 2026-09-27). The console is therefore only reachable from
  inside the distro until this is fixed (mirrored networking or a
  `wsl --shutdown` relay reset — host owner's call).
- **W-0 and W-7 pending** — James WebAdmin stabilization and the final
  acceptance-criteria verification are not yet done (see Module map).
