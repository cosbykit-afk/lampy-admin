#!/usr/bin/env python3
"""Lampy web console — phase 1 (RAD).

One tabbed web app for operating the Lampy forum stack on Toetop, so Kit
never needs a command prompt or WSL:

  Stack   — seven service cards with real TCP/HTTP/banner probes, plus
            supervisor state, per-service restart, log viewer, and
            stack start/stop/restart-all (admins only)
  Forum   — read categories / threads / posts (same queries as the app)
  Write   — new thread + reply (same validation + forum_events as the app)
  Search  — keyword search (same ILIKE query as the app); semantic is a
            labeled stub, exactly like the forum app's vector_search()
  Docs    — the forum DB docs table (body + diagram images)
  Metrics — forum_daily continuous aggregate
  Mail    — James IMAP inbox + SMTP send (mailbox credentials in a
            server-side in-memory vault, never in the session cookie)
  Admin   — users list, thread lock toggle, pg_dump backup download,
            link to code-server (admin accounts only)

SSH admin (in addition to the web UI): an embedded asyncssh server on
127.0.0.1:8022 (see ssh_admin.py) with key-based auth against Kit's
existing Windows authorized_keys and a restricted exec-only command
vocabulary covering every admin operation (status, service
start/stop/restart, logs, users, thread lock, DB backup, metrics).
No Remote Desktop and no WSL terminal needed.

Logon uses forum accounts (users table, Werkzeug hashes). Admin areas
require the forum's is_admin flag.

Deployment: runs INSIDE the `lampy` WSL distro as a supervisord program
([program:console] in /etc/supervisor/conf.d/lampy.conf), as root so it
can drive supervisorctl and read /var/log/supervisor/. Served on
127.0.0.1:8090 — WSL2 forwards distro localhost to Windows localhost,
so Kit opens http://localhost:8090. No Docker involved.

Environment (set in the supervisord program section):
    FORUM_DB_HOST=127.0.0.1 FORUM_DB_PORT=5432 FORUM_DB_NAME=forum
    FORUM_DB_USER=forum FORUM_DB_PASS=<from the distro conf>
    FORUM_SECRET_KEY=<random hex> CONSOLE_PORT=8090
"""

import csv
import io
import os
import re
import secrets
import subprocess
import threading
from datetime import datetime, timezone

from flask import (Flask, Response, abort, jsonify, make_response, redirect,
                   render_template, request, session, url_for)

import auth
import db
import dbbrowser
import mail as mailmod
import security
import stack as stackmod

app = Flask(__name__)
app.secret_key = security.load_secret_key()
security.configure_session_cookies(app)

login_limiter = security.LoginRateLimiter()
CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "8090"))
CONSOLE_BIND = os.environ.get("CONSOLE_BIND", "127.0.0.1")


@app.context_processor
def _inject():
    ep = (request.endpoint or "")
    tab = ""
    for prefix, name in (("stack", "stack"), ("forum", "forum"),
                         ("write", "write"), ("search", "search"),
                         ("doc", "docs"), ("metrics", "metrics"),
                         ("mail", "mail"), ("admin", "admin"), ("db", "db")):
        if ep.startswith(prefix):
            tab = name
            break
    return {"console_user": auth.current_console_user(),
            "csrf_token": security.csrf_token(),
            "tab": tab}


def _db_error(e, what):
    return render_template(
        "error.html", title="Database unavailable",
        message="Could not %s: %s: %s"
                % (what, type(e).__name__, e)), 503


# --------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------

@app.get("/")
def home():
    return redirect("/stack")


@app.get("/stack")
def stack_page():
    return render_template("stack.html", cards=stackmod.probe_all())


@app.get("/api/stack")
def stack_api():
    return jsonify(stackmod.probe_all())


@app.post("/stack/control/<name>/<action>")
@auth.admin_required
def stack_control(name, action):
    """Per-service restart/stop/start via supervisorctl (admins only)."""
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    ok, msg = stackmod.supervisor_control(action, name)
    return render_template("stack_result.html", ok=ok, message=msg,
                           name=name, action=action)


@app.post("/stack/control-all/<action>")
@auth.admin_required
def stack_control_all(action):
    """Start/stop/restart the seven stack programs (admins only).

    The console itself is deliberately excluded: stopping it mid-request
    would kill the response, and restarting it drops the connection.
    Use the separate "Restart console" button for that (it warns first).
    """
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    if action not in ("start", "stop", "restart"):
        abort(400, "unknown action")
    results = []
    for name in stackmod.STACK_SERVICES:
        ok, msg = stackmod.supervisor_control(action, name)
        results.append({"name": name, "ok": ok, "message": msg})
    return render_template("stack_result.html", action=action,
                           all_results=results)


@app.get("/stack/logs/<name>")
@auth.admin_required
def stack_logs(name):
    """Tail of a service's supervisord log (admins only)."""
    try:
        lines = max(50, min(2000, int(request.args.get("lines", "200"))))
    except ValueError:
        lines = 200
    ok, text = stackmod.read_service_log(name, lines)
    if not ok and text.startswith("unknown program"):
        abort(404)
    return render_template("logs.html", name=name, ok=ok, text=text,
                           lines=lines)


# --------------------------------------------------------------------------
# Database (generic PostgreSQL browser — admin only)
#
# One tab for every PostgreSQL database on the server (DB-01..DB-13).
# Database/schema/table names come only from the live catalog via
# dbbrowser (never hardcoded, never trusted from the URL unvalidated).
# --------------------------------------------------------------------------

@app.get("/db")
@auth.admin_required
def db_index():
    """Database selector (DB-02)."""
    try:
        databases = dbbrowser.list_databases()
    except dbbrowser.NotConfiguredError as e:
        return render_template(
            "error.html", title="Database administration unavailable",
            message=str(e)), 503
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "list databases")
    return render_template("db_index.html", databases=databases,
                           title="Database")


def _db_write_enabled(dbname):
    """Per-session, per-database write-mode flag (DB-07)."""
    flags = session.get("db_write")
    return bool(isinstance(flags, dict) and flags.get(dbname))


def _db_sql_context(dbname, **kw):
    ctx = {"dbname": dbname, "sql": "", "cols": [], "rows": [],
           "truncated": False, "mode": "", "ran": False, "error": "",
           "write_enabled": _db_write_enabled(dbname),
           "title": "SQL: %s" % dbname}
    ctx.update(kw)
    return ctx


def _db_not_configured(e):
    return render_template(
        "error.html", title="Database administration unavailable",
        message=str(e)), 503


@app.get("/db/<dbname>")
@auth.admin_required
def db_schemas(dbname):
    """Schema -> table tree for one database (DB-03)."""
    try:
        dbname = dbbrowser.checked_dbname(dbname)
        schemas = [(s, dbbrowser.list_tables(dbname, s))
                   for s in dbbrowser.list_schemas(dbname)]
    except LookupError:
        abort(404)
    except dbbrowser.NotConfiguredError as e:
        return _db_not_configured(e)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "browse the database")
    return render_template("db_schemas.html", dbname=dbname, schemas=schemas,
                           title="Database: %s" % dbname)


@app.get("/db/<dbname>/<schema>/<table>")
@auth.admin_required
def db_table(dbname, schema, table):
    """Table metadata + paginated row browser (DB-04, DB-05, DB-11)."""
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        abort(400, "bad page number")
    if page < 1:
        abort(400, "bad page number")
    try:
        dbname = dbbrowser.checked_dbname(dbname)
        schema, table = dbbrowser.checked_relation(dbname, schema, table)
        meta = dbbrowser.table_metadata(dbname, schema, table)
        pages = max(1, -(-max(0, meta["estimate"])
                         // dbbrowser.ROWS_PER_PAGE))
        if page > pages:
            abort(404)
        cols, rows = dbbrowser.browse_rows(dbname, schema, table, page)
    except LookupError:
        abort(404)
    except dbbrowser.NotConfiguredError as e:
        return _db_not_configured(e)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "browse the table")
    disp_rows = [[dbbrowser.display_cell(v) for v in row] for row in rows]
    return render_template("db_table.html", dbname=dbname, schema=schema,
                           table=table, meta=meta, cols=cols, rows=disp_rows,
                           page=page, pages=pages,
                           title="%s.%s.%s" % (dbname, schema, table))


@app.get("/db/<dbname>/sql")
@auth.admin_required
def db_sql_form(dbname):
    """SQL query box (DB-06, DB-07). Read-only unless write mode was
    explicitly enabled for this database in this session."""
    try:
        dbname = dbbrowser.checked_dbname(dbname)
    except LookupError:
        abort(404)
    except dbbrowser.NotConfiguredError as e:
        return _db_not_configured(e)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "open the SQL box")
    return render_template("db_sql.html", **_db_sql_context(dbname))


@app.post("/db/<dbname>/sql")
@auth.admin_required
def db_sql_run(dbname):
    """Execute one statement from the SQL box."""
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    sqltext = request.form.get("sql", "")
    try:
        dbname = dbbrowser.checked_dbname(dbname)
        cols, rows, truncated, mode = dbbrowser.run_sql(
            dbname, sqltext, allow_write=_db_write_enabled(dbname))
    except LookupError:
        abort(404)
    except (ValueError, PermissionError,
            dbbrowser.StatementTimeoutError) as e:
        return render_template(
            "db_sql.html",
            **_db_sql_context(dbname, sql=sqltext,
                              error=str(e))), 400
    except dbbrowser.NotConfiguredError as e:
        return _db_not_configured(e)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "run the SQL")
    disp_rows = [[dbbrowser.display_cell(v) for v in row] for row in rows]
    return render_template(
        "db_sql.html",
        **_db_sql_context(dbname, sql=sqltext, cols=cols, rows=disp_rows,
                          truncated=truncated, mode=mode, ran=True))


@app.post("/db/<dbname>/sql/enable-write")
@auth.admin_required
def db_sql_enable_write(dbname):
    """Enable write mode for this database — ONLY with the explicit
    confirmation checkbox (DB-07). Per-session; dies on logout."""
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    try:
        dbname = dbbrowser.checked_dbname(dbname)
    except LookupError:
        abort(404)
    except dbbrowser.NotConfiguredError as e:
        return _db_not_configured(e)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "open the SQL box")
    if request.form.get("confirm") != "yes":
        abort(400, "Write mode requires explicit confirmation.")
    flags = dict(session.get("db_write") or {})
    flags[dbname] = True
    session["db_write"] = flags
    return redirect("/db/%s/sql" % dbname)


@app.post("/db/<dbname>/sql/disable-write")
@auth.admin_required
def db_sql_disable_write(dbname):
    """Drop write mode for this database."""
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    try:
        dbname = dbbrowser.checked_dbname(dbname)
    except LookupError:
        abort(404)
    flags = dict(session.get("db_write") or {})
    flags.pop(dbname, None)
    session["db_write"] = flags
    return redirect("/db/%s/sql" % dbname)


@app.post("/db/<dbname>/export")
@auth.admin_required
def db_export(dbname):
    """Stream query results as CSV (DB-12). Read-only always — the
    classifier must say "read"; write mode is never honored here."""
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    try:
        dbname = dbbrowser.checked_dbname(dbname)
    except LookupError:
        abort(404)
    sqltext = request.form.get("sql", "")
    if dbbrowser.classify_statement(sqltext) != "read":
        abort(400, "Export is read-only.")
    # Prime the first batch so a bad query fails cleanly (400) instead of
    # dying mid-download.
    stream = dbbrowser.stream_sql_batches(dbname, sqltext)
    try:
        primed = [next(stream)]
    except StopIteration:
        primed = []
    except dbbrowser.NotConfiguredError as e:
        return _db_not_configured(e)
    except Exception as e:  # noqa: BLE001
        return render_template(
            "error.html", title="Export failed",
            message="%s: %s" % (type(e).__name__, e)), 400

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        first = True

        def all_batches():
            for b in primed:
                yield b
            for b in stream:
                yield b

        for cols, batch in all_batches():
            if first:
                writer.writerow(cols)
                first = False
            for row in batch:
                writer.writerow([dbbrowser.cell_text(v) for v in row])
            chunk = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            if chunk:
                yield chunk

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    fname = "db-%s-%s.csv" % (re.sub(r"[^A-Za-z0-9_]", "_", dbname), stamp)
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=" + fname})


# --------------------------------------------------------------------------
# Forum (read)
# --------------------------------------------------------------------------

@app.get("/forum")
def forum_index():
    try:
        cats = db.get_categories()
        latest = db.get_latest_threads()
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the forum index")
    return render_template("forum_index.html", cats=cats, latest=latest)


@app.get("/forum/category/<int:cat_id>")
def forum_category(cat_id):
    try:
        cat, threads = db.get_category(cat_id)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the category")
    if cat is None:
        abort(404)
    return render_template("forum_category.html", cat=cat, threads=threads)


@app.get("/forum/thread/<int:thread_id>")
def forum_thread(thread_id):
    page = request.args.get("page", "1")
    try:
        page = max(1, int(page))
    except ValueError:
        page = 1
    try:
        th, posts, page, pages = db.get_thread(thread_id, page)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the thread")
    if th is None:
        abort(404)
    return render_template("forum_thread.html", th=th, posts=posts,
                           page=page, pages=pages)


# --------------------------------------------------------------------------
# Write (login required; mirrors app.py validation + events)
# --------------------------------------------------------------------------

@app.get("/write")
@auth.login_required
def write_form():
    try:
        cats = db.get_categories()
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load categories")
    return render_template("write.html", cats=cats, error="",
                           title="", body="",
                           cat_id=request.args.get("category", ""))


@app.post("/write")
@auth.login_required
def write_post():
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    user = auth.current_console_user()
    try:
        cat_id = int(request.form.get("category_id", "0"))
    except ValueError:
        cat_id = 0
    title = request.form.get("title", "")
    body = request.form.get("body", "")
    try:
        tid, err = db.create_thread(cat_id, user["id"], title, body)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "create the thread")
    if err:
        try:
            cats = db.get_categories()
        except Exception:  # noqa: BLE001
            cats = []
        return render_template("write.html", cats=cats, error=err,
                               title=title, body=body,
                               cat_id=str(cat_id)), 400
    return redirect("/forum/thread/%d" % tid)


@app.post("/write/reply/<int:thread_id>")
@auth.login_required
def write_reply(thread_id):
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    user = auth.current_console_user()
    body = request.form.get("body", "")
    try:
        err = db.create_reply(thread_id, user["id"], body)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "post the reply")
    if err:
        return render_template("error.html", title="Could not post reply",
                               message=err), 400
    return redirect("/forum/thread/%d" % thread_id)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    results, vec = [], db.vectorizer_status()
    if q:
        try:
            results = db.keyword_search(q)
        except Exception as e:  # noqa: BLE001
            return _db_error(e, "run the search")
    return render_template("search.html", q=q, results=results,
                           mode="keyword", vec_note=vec["note"])


# --------------------------------------------------------------------------
# Docs
# --------------------------------------------------------------------------

@app.get("/docs")
def docs_list():
    try:
        docs = db.get_docs()
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the docs list")
    return render_template("docs.html", docs=docs)


@app.get("/docs/<slug>")
def doc_view(slug):
    try:
        doc = db.get_doc(slug)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the doc")
    if doc is None:
        abort(404)
    return render_template("doc_view.html", doc=doc)


@app.get("/docs/<slug>/image")
def doc_image(slug):
    try:
        blob, mime = db.get_doc_image(slug)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the doc image")
    if blob is None:
        abort(404)
    return Response(blob, mimetype=mime or "application/octet-stream")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

@app.get("/metrics")
def metrics():
    try:
        rows = db.get_metrics()
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load metrics")
    days = {}
    for r in rows:
        days.setdefault(str(r["day"]), {})[r["event_type"]] = r["n"]
    ordered = sorted(days.items(), reverse=True)
    return render_template("metrics.html", days=ordered)


# --------------------------------------------------------------------------
# Mail (James IMAP/SMTP; mailbox credentials in a server-side vault)
# --------------------------------------------------------------------------

# In-memory vault: random token -> {"user":..., "password":...}. The Flask
# session cookie only carries the token (Flask cookies are signed, not
# encrypted), so a mailbox password never leaves this process.
_MAIL_VAULT = {}


def _mail_creds():
    tok = session.get("mail_token")
    if not tok:
        return None
    return _MAIL_VAULT.get(tok)


def _drop_mail_creds():
    tok = session.pop("mail_token", None)
    if tok:
        _MAIL_VAULT.pop(tok, None)


@app.get("/mail")
@auth.login_required
def mail_index():
    creds = _mail_creds()
    if not creds:
        return render_template("mail.html", connected=False, error="")
    try:
        msgs = mailmod.list_messages(creds["user"], creds["password"])
    except Exception as e:  # noqa: BLE001
        return render_template("mail.html", connected=False,
                               error=str(e),
                               hint="If James has no mailbox for this name "
                                    "yet, one has to be provisioned on the "
                                    "mail server first — the console never "
                                    "invents mailbox data.")
    return render_template("mail.html", connected=True, msgs=msgs,
                           user=creds["user"], error="")


@app.post("/mail/connect")
@auth.login_required
def mail_connect():
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    user = request.form.get("mail_user", "").strip()
    password = request.form.get("mail_password", "")
    if not user or not password:
        return render_template("mail.html", connected=False,
                               error="Username and password are required.")
    try:
        mailmod.list_messages(user, password, limit=1)
    except Exception as e:  # noqa: BLE001
        return render_template("mail.html", connected=False, error=str(e),
                               hint="If James has no mailbox for this name "
                                    "yet, one has to be provisioned on the "
                                    "mail server first — the console never "
                                    "invents mailbox data.")
    session["mail_token"] = token = secrets.token_hex(16)
    _MAIL_VAULT[token] = {"user": user, "password": password}
    return redirect("/mail")


@app.post("/mail/disconnect")
@auth.login_required
def mail_disconnect():
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    _drop_mail_creds()
    return redirect("/mail")


@app.get("/mail/<num>")
@auth.login_required
def mail_view(num):
    creds = _mail_creds()
    if not creds:
        return redirect("/mail")
    try:
        msg = mailmod.fetch_message(creds["user"], creds["password"], num)
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", title="Could not open message",
                               message="%s: %s"
                               % (type(e).__name__, e)), 502
    return render_template("mail_view.html", msg=msg)


@app.get("/mail/compose")
@auth.login_required
def mail_compose():
    if not _mail_creds():
        return redirect("/mail")
    return render_template("mail_compose.html", error="")


@app.post("/mail/send")
@auth.login_required
def mail_send():
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    creds = _mail_creds()
    if not creds:
        return redirect("/mail")
    to_addr = request.form.get("to", "").strip()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "")
    if not to_addr or not body:
        return render_template("mail_compose.html",
                               error="A recipient and a body are required.")
    try:
        mailmod.send_message(creds["user"], creds["password"],
                             to_addr, subject, body)
    except Exception as e:  # noqa: BLE001
        return render_template("mail_compose.html", error=str(e)), 502
    return redirect("/mail")


# --------------------------------------------------------------------------
# Admin (admin accounts only)
# --------------------------------------------------------------------------

@app.get("/admin")
@auth.admin_required
def admin():
    try:
        users = db.get_users()
        counts = db.get_table_counts()
        threads = db.get_threads_for_admin()
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "load the admin page")
    return render_template("admin.html", users=users, counts=counts,
                           threads=threads)


@app.post("/admin/lock/<int:thread_id>")
@auth.admin_required
def admin_lock(thread_id):
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    locked = request.form.get("locked") == "1"
    try:
        db.set_thread_locked(thread_id, locked)
    except Exception as e:  # noqa: BLE001
        return _db_error(e, "update the thread lock")
    return redirect("/admin")


@app.get("/admin/backup")
@auth.admin_required
def admin_backup():
    """pg_dump over distro-localhost TCP (/usr/bin/pg_dump is present in
    the lampy distro). Streams the dump as a download. Uses set -o
    pipefail semantics: the return code is the dump's own, never the
    pipe's."""
    env = dict(os.environ)
    env["PGPASSWORD"] = db.DB_PASS
    try:
        p = subprocess.run(
            ["pg_dump", "-h", db.DB_HOST, "-p", str(db.DB_PORT),
             "-U", db.DB_USER, "-d", db.DB_NAME],
            capture_output=True, env=env, timeout=600)
    except FileNotFoundError:
        return render_template(
            "error.html", title="Backup unavailable",
            message="pg_dump is not installed in the console image."), 500
    except subprocess.TimeoutExpired:
        return render_template(
            "error.html", title="Backup timed out",
            message="pg_dump did not finish within 10 minutes."), 504
    if p.returncode != 0:
        return render_template(
            "error.html", title="Backup failed",
            message="pg_dump exited %d: %s"
                    % (p.returncode,
                       p.stderr.decode("utf-8", "replace")[:500])), 500
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    resp = make_response(p.stdout)
    resp.headers["Content-Type"] = "application/octet-stream"
    resp.headers["Content-Disposition"] = \
        "attachment; filename=lampy-forum-backup-%s.sql" % stamp
    return resp


# --------------------------------------------------------------------------
# Gwen chat (W-2) — natural-language admin via Ollama
# --------------------------------------------------------------------------

@app.get("/gwen")
@auth.admin_required
def gwen_page():
    """Gwen chat tab."""
    return render_template("gwen.html", tab="gwen")


@app.post("/api/gwen/chat")
@auth.admin_required
def gwen_chat_api():
    """JSON chat endpoint. Returns {"response": str} or
    {"response": str, "pending_action": {...}} when Gwen proposes a
    control action (Kit confirms via /api/gwen/confirm)."""
    if not security.validate_csrf_json():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    history = data.get("history", [])
    if not isinstance(history, list):
        history = []
    try:
        import gwen_chat
        result = gwen_chat.chat(message, history,
                                auth.current_console_user())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "chat failed: %s" % e}), 500
    out = {"response": result.get("text", "")}
    if result.get("pending_action"):
        out["pending_action"] = result["pending_action"]
    return jsonify(out)


@app.post("/api/gwen/confirm")
@auth.admin_required
def gwen_confirm():
    """Execute a pending control action. The ONLY code path from a
    pending-action token to supervisor_control.

    Requires: valid single-use token, the issuing admin's session, and a
    fresh JSON CSRF token. Re-checks the target (never "console") even
    with a valid token."""
    if not security.validate_csrf_json():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    import gwen_chat
    rec = gwen_chat.consume_pending_action(token)
    if rec is None:
        return jsonify({"error": "unknown or expired token"}), 410
    me = auth.current_console_user() or {}
    if rec["user_id"] != me.get("id"):
        return jsonify({"error": "token bound to another session"}), 403
    # Defense in depth: re-check the target even with a valid token.
    if rec["name"] == "console" or rec["name"] not in gwen_chat.MANAGEABLE:
        gwen_chat.audit_log("refused", user=me, action=rec["action"],
                            target=rec["name"],
                            token_id=rec["token_id"],
                            reason="target re-check failed")
        return jsonify({"error": "refused"}), 403
    ok, output = stackmod.supervisor_control(rec["action"], rec["name"])
    gwen_chat.audit_log("executed", user=me, action=rec["action"],
                        target=rec["name"], token_id=rec["token_id"], ok=ok)
    return jsonify({"ok": ok, "message": output,
                    "action": rec["action"], "name": rec["name"]})


# --------------------------------------------------------------------------
# Ollama model management (W-3) — extends the Gwen tab
# --------------------------------------------------------------------------

@app.get("/api/gwen/models")
@auth.admin_required
def gwen_models():
    """Installed models + disk usage + Gwen's current model (OLL-01/02/05).
    Never blank: Ollama-down returns 502 with a clear error (OLL-08)."""
    import gwen_chat
    import ollama_admin
    ok, models = ollama_admin.list_models()
    if not ok:
        return jsonify({"error": models}), 502
    ok2, total = ollama_admin.disk_usage()
    return jsonify({
        "models": models,
        "disk_total_bytes": total if ok2 else 0,
        "disk_total": ollama_admin.format_bytes(total) if ok2 else "?",
        "gwen_model": gwen_chat.get_model(),
    })


@app.post("/api/gwen/model")
@auth.admin_required
def gwen_set_model():
    """Switch the model Gwen chats with (OLL-02). The name must be
    installed — validated against /api/tags first."""
    if not security.validate_csrf_json():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    import gwen_chat
    import ollama_admin
    data = request.get_json(silent=True) or {}
    name = (data.get("model") or "").strip()
    ok, models = ollama_admin.list_models()
    if not ok:
        return jsonify({"error": models}), 502
    if name not in [m["name"] for m in models]:
        return jsonify({"error": "model not installed: %s" % name}), 400
    ok, msg = gwen_chat.set_model(name)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 500


@app.post("/api/gwen/models/pull")
@auth.admin_required
def gwen_pull_model():
    """Start pulling a model in the background (OLL-03)."""
    if not security.validate_csrf_json():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    import ollama_admin
    data = request.get_json(silent=True) or {}
    ok, msg = ollama_admin.pull_model(data.get("name", ""))
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 400


@app.get("/api/gwen/models/pull-status")
@auth.admin_required
def gwen_pull_status():
    """Poll pull progress (OLL-03)."""
    import ollama_admin
    ok, state = ollama_admin.pull_status(request.args.get("name", ""))
    return jsonify(state if ok else {"error": state}), 200 if ok else 404


@app.post("/api/gwen/models/delete")
@auth.admin_required
def gwen_delete_model():
    """Delete a model (OLL-04). The UI confirms explicitly first; this
    endpoint also refuses to delete Gwen's current model."""
    if not security.validate_csrf_json():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    import gwen_chat
    import ollama_admin
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if name == gwen_chat.get_model():
        return jsonify({"error": "refusing to delete the model Gwen is "
                                 "currently using"}), 400
    ok, msg = ollama_admin.delete_model(name)
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 502


@app.post("/api/gwen/models/test-chat")
@auth.admin_required
def gwen_test_chat():
    """One-shot prompt against any installed model (OLL-06)."""
    if not security.validate_csrf_json():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    import ollama_admin
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    if not model or not prompt:
        return jsonify({"error": "model and prompt are required"}), 400
    ok, text = ollama_admin.test_chat(model, prompt)
    return jsonify({"response": text} if ok else {"error": text}), \
        200 if ok else 502


# --------------------------------------------------------------------------
# Logon — forum accounts (users table, Werkzeug hashes)
# --------------------------------------------------------------------------

@app.get("/login")
def login():
    if auth.current_console_user():
        return redirect("/stack")
    return render_template("login.html", next=request.args.get("next", ""),
                           error="")


@app.post("/login")
def login_post():
    if auth.current_console_user():
        return redirect("/stack")
    ip = security.client_ip()
    retry_after = login_limiter.check(ip)
    if retry_after is not None:
        resp = make_response(render_template(
            "login.html", next=request.form.get("next", ""),
            error="Too many login attempts — try again in %d seconds."
                  % retry_after), 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    if not security.validate_csrf_form():
        abort(400, "CSRF token missing or invalid")
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    nxt = request.form.get("next", "") or "/stack"
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = "/stack"  # relative-path redirects only
    try:
        user = auth.verify_login(username, password)
    except RuntimeError as e:
        # Honest failure: the account database is down, not the password.
        return render_template("login.html", next=nxt,
                               error=str(e)), 503
    if user is None:
        login_limiter.record_failure(ip)
        return render_template("login.html", next=nxt,
                               error="Invalid username or password."), 401
    login_limiter.record_success(ip)
    security.rotate_csrf_token()
    session["console_user"] = user
    return redirect(nxt)


@app.post("/logout")
def logout():
    # POST only: a GET link could be triggered by a third-party page.
    session.pop("console_user", None)
    _drop_mail_creds()
    return redirect("/stack")


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", title="Not found",
                           message="That page does not exist."), 404


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


def _start_ssh_server():
    """Run the embedded SSH admin server (auxiliary — the web UI must
    survive its failure)."""
    try:
        import ssh_admin
        ssh_admin.start_ssh_server()  # blocks forever
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("console").error("SSH admin server failed: %s", e)


if __name__ == "__main__":
    # DB self-test on startup (RAD SDLC) - logs result, never crashes
    try:
        ok, err = db.db_ping()
        if ok:
            print("db-selftest: OK - database reachable", flush=True)
        else:
            print("db-selftest: FAIL - %s" % err, flush=True)
    except Exception as e:
        print("db-selftest: ERROR - %s: %s" % (type(e).__name__, e), flush=True)
    _ssh_thread = threading.Thread(target=_start_ssh_server, daemon=True,
                                   name="ssh-admin")
    _ssh_thread.start()
    app.run(host=CONSOLE_BIND, port=CONSOLE_PORT)
