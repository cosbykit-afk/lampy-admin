#!/usr/bin/env python3
"""W-1 live verification battery for the Lampy Administration App.

Runs INSIDE the `lampy` distro as root (it reads DBADMIN_* from the
root-only supervisor conf). Two layers:

  Layer A (module): imports dbbrowser directly — discovery, allowlist,
      metadata, row browsing (table AND view), SQL read/write/timeout,
      CSV export, injection attempts, secret-leak scan.
  Layer B (HTTP, no login needed): anonymous access redirects to login;
      state-changing POSTs without CSRF are rejected.

Authenticated browser steps (needs Kit's forum admin login) are printed
as a manual checklist at the end — they cannot be automated without
his credentials.

Usage (from the build VM):
  toetop-ssh-kitco.sh "wsl -d lampy -u root -- python3 - < live_w1_battery.py"
or copy to the distro and run:  python3 live_w1_battery.py

Exit 0 = all automated checks passed. Any failure raises AssertionError
with the check name.
"""

import os
import re
import sys
import time
import urllib.request
import urllib.error

CONF = "/etc/supervisor/conf.d/lampy.conf"
BASE = "http://127.0.0.1:8090"
SCRATCH = "w1_battery_scratch"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (" — " + detail if detail and not cond else ""))
    if not cond:
        raise AssertionError("FAILED: %s %s" % (name, detail))


def load_dbadmin_env():
    """DBADMIN_* from the [program:console] environment line."""
    text = open(CONF).read()
    m = re.search(r"\[program:console\].*?^environment=(.*)$",
                  text, re.S | re.M)
    assert m, "no [program:console] environment in conf"
    env_line = m.group(1)
    # environment may span logical continuations; join backslash-newlines
    env_line = env_line.replace("\\\n", "")
    for key in ("DBADMIN_HOST", "DBADMIN_PORT",
                "DBADMIN_USER", "DBADMIN_PASS"):
        mm = re.search(r'%s="([^"]*)"' % key, env_line)
        assert mm, "missing %s in console env" % key
        os.environ[key] = mm.group(1)


def main():
    load_dbadmin_env()
    sys.path.insert(0, "/opt/lampy-console")
    import dbbrowser
    import psycopg

    admin = dict(host=os.environ["DBADMIN_HOST"],
                 port=int(os.environ["DBADMIN_PORT"]),
                 user=os.environ["DBADMIN_USER"],
                 password=os.environ["DBADMIN_PASS"])

    def pg(dbname):
        return psycopg.connect(dbname=dbname, **admin)

    # ---- setup: scratch DB FIRST, before any write-mode use ----
    # (CREATE/DROP DATABASE cannot run in a transaction block: autocommit)
    with psycopg.connect(dbname="postgres", autocommit=True, **admin) as conn:
        conn.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
        conn.execute("CREATE DATABASE %s" % SCRATCH)

    try:
        with pg(SCRATCH) as conn:
            conn.execute("CREATE TABLE items(id serial primary key, "
                         "name text, payload bytea)")
            conn.execute("INSERT INTO items(name, payload) VALUES "
                         "('alpha', '\\x00ff'), ('<b>beta</b>', NULL), "
                         "('gamma', '\\xdeadbeef')")
            conn.execute("CREATE VIEW item_names AS SELECT name FROM items")
            conn.commit()

        # ---- DB1: live database discovery ----
        dbs = dbbrowser.list_databases()
        check("DB1-discovery", "postgres" in dbs and "forum" in dbs
              and SCRATCH in dbs, str(dbs))

        # ---- DB2: schema/table discovery ----
        schemas = dbbrowser.list_schemas(SCRATCH)
        check("DB2-schemas", "public" in schemas, str(schemas))
        tables = dbbrowser.list_tables(SCRATCH, "public")
        names = [t[0] for t in tables]
        kinds = dict((t[0], t[1]) for t in tables)
        check("DB2-tables", "items" in names and "item_names" in names,
              str(tables))
        check("DB2-view-kind", kinds.get("item_names") == "VIEW",
              str(kinds))

        # ---- DB3: metadata ----
        meta = dbbrowser.table_metadata(SCRATCH, "public", "items")
        check("DB3-metadata",
              any(c["name"] == "id" for c in meta["columns"])
              and meta["estimate"] >= 3, str(meta["estimate"]))

        # ---- DB4: row browsing — table (ctid order) AND view (no ctid) ----
        cols, rows = dbbrowser.browse_rows(SCRATCH, "public", "items", 1)
        check("DB4-table-rows", cols == ["id", "name", "payload"]
              and len(rows) == 3, str(cols))
        cols_v, rows_v = dbbrowser.browse_rows(
            SCRATCH, "public", "item_names", 1)
        check("DB4-view-rows", cols_v == ["name"] and len(rows_v) == 3,
              str(cols_v))

        # ---- hostile HTML / NULL / bytea rendering ----
        check("DB4-hostile-cell",
              dbbrowser.display_cell("<b>beta</b>") == "<b>beta</b>",
              "must NOT pre-escape (template escapes)")
        check("DB4-null-cell", dbbrowser.display_cell(None) is None)
        check("DB4-bytea-cell",
              dbbrowser.display_cell(b"\xde\xad\xbe\xef") == "deadbeef")

        # ---- DB5: SQL read path ----
        cols, rows, trunc, mode = dbbrowser.run_sql(
            SCRATCH, "SELECT name FROM items ORDER BY id")
        check("DB5-select", mode == "read" and len(rows) == 3
              and rows[0][0] == "alpha", str(rows))

        # ---- DB6: write path — read-only refusal, then confirmed CRUD ----
        try:
            dbbrowser.run_sql(
                SCRATCH, "INSERT INTO items(name) VALUES ('x')")
            check("DB6-readonly-refusal", False, "write was NOT refused")
        except PermissionError:
            check("DB6-readonly-refusal", True)
        cols, rows, trunc, mode = dbbrowser.run_sql(
            SCRATCH, "INSERT INTO items(name) VALUES ('delta') "
            "RETURNING id", allow_write=True)
        check("DB6-write-insert", mode == "write" and len(rows) == 1,
              str(rows))
        cols, rows, trunc, mode = dbbrowser.run_sql(
            SCRATCH, "CREATE TABLE no_result(a int)", allow_write=True)
        check("DB6-write-noresult",
              (cols, rows, trunc, mode) == ([], [], False, "write"),
              "fetch on no-result statement must not raise")
        cols, rows, trunc, mode = dbbrowser.run_sql(
            SCRATCH, "SELECT count(*) FROM items", allow_write=True)
        check("DB6-write-verify", rows[0][0] == 4, str(rows))

        # ---- TIMEOUT: pg_sleep beyond the statement timeout ----
        t0 = time.time()
        try:
            dbbrowser.run_sql(SCRATCH, "SELECT pg_sleep(45)")
            check("DB6-timeout", False, "pg_sleep(45) was NOT cancelled")
        except dbbrowser.StatementTimeoutError:
            dt = time.time() - t0
            check("DB6-timeout", dt < 40, "cancelled after %.1fs" % dt)

        # ---- INJ: URL injection attempts ----
        for evil in ["items'; DROP TABLE items;--", "../..",
                     "pg_catalog.pg_class", "items\""]:
            try:
                dbbrowser.checked_relation(SCRATCH, "public", evil)
                check("INJ-relation", False, "accepted %r" % evil)
            except LookupError:
                pass
        check("INJ-relation", True)
        try:
            dbbrowser.checked_schema(SCRATCH, "pg_catalog")
            check("INJ-schema", False, "pg_catalog accepted")
        except LookupError:
            check("INJ-schema", True)

        # ---- PERMS: export is read-only even in write mode ----
        try:
            list(dbbrowser.stream_sql_batches(
                SCRATCH, "DELETE FROM items"))
            check("PERMS-export-readonly", False, "export ran a write")
        except PermissionError:
            check("PERMS-export-readonly", True)

        # ---- EXP: CSV export ----
        batches = list(dbbrowser.stream_sql_batches(
            SCRATCH, "SELECT id, name FROM items ORDER BY id"))
        n_rows = sum(len(r) for _, r in batches)
        check("EXP-csv", n_rows == 4 and batches[0][0] == ["id", "name"],
              "%d rows" % n_rows)

        # ---- SECRET: password must not appear in any output ----
        secret = os.environ["DBADMIN_PASS"]
        assert len(secret) >= 8, "refusing to scan with a trivial password"
        haystacks = []
        bcols, brows = dbbrowser.browse_rows(SCRATCH, "public", "items", 1)
        haystacks.append(str(bcols) + str(brows))
        cols, rows, _, _ = dbbrowser.run_sql(
            SCRATCH, "SELECT name FROM items")
        haystacks.append(str(rows))
        for bcols, brows in dbbrowser.stream_sql_batches(
                SCRATCH, "SELECT name FROM items"):
            haystacks.append(str(brows))
        leaked = [h for h in haystacks if secret in h]
        check("SECRET-no-leak", not leaked, str(leaked)[:100])

        # ---- Layer B: HTTP negatives (no login) ----
        def get(path):
            try:
                r = urllib.request.urlopen(BASE + path, timeout=10)
                return r.status, r.headers.get("Location", "")
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Location", "")

        def post(path, data=b""):
            req = urllib.request.Request(BASE + path, data=data,
                                         method="POST")
            try:
                r = urllib.request.urlopen(req, timeout=10)
                return r.status
            except urllib.error.HTTPError as e:
                return e.code

        for p in ["/db", "/db/%s" % SCRATCH,
                  "/db/%s/public/items" % SCRATCH]:
            status, loc = get(p)
            check("HTTP-anon-redirect %s" % p,
                  status in (301, 302, 303) and "/login" in loc,
                  "got %d -> %s" % (status, loc))
        check("HTTP-csrf-sql",
              post("/db/%s/sql" % SCRATCH, b"q=SELECT+1") in (400, 403),
              "write POST without CSRF must be rejected")
        check("HTTP-csrf-export",
              post("/db/%s/export" % SCRATCH, b"q=SELECT+1") in (400, 403))

    finally:
        # ---- guaranteed scratch cleanup ----
        with psycopg.connect(dbname="postgres", autocommit=True,
                             **admin) as conn:
            conn.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
        print("scratch database dropped")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    print("""
MANUAL CHECKLIST (needs Kit's forum admin login, from his browser):
 [ ] Log in at http://localhost:8090, open the Database tab
 [ ] /db lists postgres, forum (+ any others) — no hardcoded names
 [ ] Browse forum.public.<a table>: columns, rows, pagination
 [ ] SQL tab: SELECT 1 -> 1 row; DROP TABLE x -> refused in read-only mode
 [ ] Enable write mode for a scratch DB, run CREATE/INSERT, disable after
 [ ] Export CSV of a SELECT; confirm write statements are refused for export
 [ ] Keyboard: tab through the DB tab — every control reachable, no traps
""")


if __name__ == "__main__":
    main()
