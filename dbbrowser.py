"""Generic PostgreSQL browser for the Lampy web console (Database tab).

Contract (see Management_App_Requirements.md DB-01..DB-13 and
Database_Tab_Implementation.md):
  - Works against ANY database on the server. Database names come only
    from pg_database (list_databases); no names are hardcoded here.
  - Read-only by default. classify_statement() gates every statement:
    "read" runs, "write" needs explicit per-session write mode,
    "invalid" is refused outright. The read path additionally runs inside
    SET TRANSACTION READ ONLY (defense in depth under the classifier).
  - Every statement carries SET LOCAL statement_timeout = '30s'.
  - Credentials come from the DBADMIN_* environment (supervisord
    [program:console] environment= line, mode 600). The conninfo string
    is never logged, printed, or placed in an exception/template.

Declarations first: configuration, vocabulary, then the DB layer, then
the classifier, then execution. No Flask imports in this module.
"""

import os
import re

import psycopg
import psycopg.errors
from psycopg import sql

# --------------------------------------------------------------------------
# Configuration (environment; read once at import, like db.py)
# --------------------------------------------------------------------------

DBADMIN_HOST = os.environ.get("DBADMIN_HOST", "127.0.0.1")
DBADMIN_PORT = int(os.environ.get("DBADMIN_PORT", "5432"))
DBADMIN_USER = os.environ.get("DBADMIN_USER", "postgres")
DBADMIN_PASS = os.environ.get("DBADMIN_PASS", "")

STATEMENT_TIMEOUT = "30s"
ROWS_PER_PAGE = 100
RESULT_ROW_CAP = 1000
EXPORT_BATCH = 500

# --------------------------------------------------------------------------
# Statement classifier vocabulary
# --------------------------------------------------------------------------

# First words that are reads. Everything else is "write" (default-deny);
# anything with a statement separator outside a literal is "invalid".
READ_FIRST = frozenset({"select", "with", "values", "table", "explain"})

# Identifier allowlist for database/schema/table names coming from URLs.
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class NotConfiguredError(Exception):
    """DBADMIN credentials are not present in the environment."""


class StatementTimeoutError(Exception):
    """A statement exceeded the statement_timeout (SQLSTATE 57014)."""


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

def _require_config():
    if not DBADMIN_PASS:
        raise NotConfiguredError(
            "Database administration is not configured: DBADMIN_PASS "
            "is not set in the console's supervisord environment.")


def _admin_conninfo(dbname):
    # Built here and passed straight to psycopg.connect. This string is
    # NEVER logged, printed, or included in an exception message.
    return (
        "host=%s port=%d dbname=%s user=%s password=%s connect_timeout=5"
        % (DBADMIN_HOST, DBADMIN_PORT, dbname, DBADMIN_USER, DBADMIN_PASS)
    )


def _connect(dbname):
    """psycopg connection as context manager (auto-rollback on error)."""
    _require_config()
    return psycopg.connect(_admin_conninfo(dbname))


# --------------------------------------------------------------------------
# Discovery (allowlisted — the ONLY source of names in the UI)
# --------------------------------------------------------------------------

def list_databases():
    """All connectable non-template databases, alphabetically (DB-02)."""
    with _connect("postgres") as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database "
            "WHERE NOT datistemplate AND datallowconn "
            "ORDER BY datname").fetchall()
    return [r[0] for r in rows]


def checked_dbname(dbname):
    """Allowlist gate for a database name from a URL (DB-10).

    Raises LookupError unless the name is a safe identifier AND a real
    database on this server.
    """
    if not isinstance(dbname, str) or not IDENT_RE.match(dbname):
        raise LookupError("unknown database: %r" % (dbname,))
    if dbname not in list_databases():
        raise LookupError("unknown database: %r" % (dbname,))
    return dbname


def list_schemas(dbname):
    """User schemas of a database (no pg_catalog / information_schema)."""
    dbname = checked_dbname(dbname)
    with _connect(dbname) as conn:
        rows = conn.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog','information_schema') "
            "AND schema_name NOT LIKE 'pg\\_toast%%' "
            "ORDER BY schema_name").fetchall()
    return [r[0] for r in rows]


def checked_schema(dbname, schema):
    """Allowlist gate for a schema name from a URL."""
    dbname = checked_dbname(dbname)
    if not isinstance(schema, str) or not IDENT_RE.match(schema):
        raise LookupError("unknown schema: %r" % (schema,))
    if schema not in list_schemas(dbname):
        raise LookupError("unknown schema: %r" % (schema,))
    return schema


def list_tables(dbname, schema):
    """(name, type) of base tables and views in a schema (DB-03)."""
    schema = checked_schema(dbname, schema)
    with _connect(dbname) as conn:
        rows = conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s "
            "AND table_type IN ('BASE TABLE','VIEW') "
            "ORDER BY table_name", (schema,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def checked_relation(dbname, schema, table):
    """Allowlist gate for a schema.table pair from a URL.

    Returns the validated (schema, table). Raises LookupError otherwise.
    """
    schema = checked_schema(dbname, schema)
    if not isinstance(table, str) or not IDENT_RE.match(table):
        raise LookupError("unknown table: %r" % (table,))
    names = [t[0] for t in list_tables(dbname, schema)]
    if table not in names:
        raise LookupError("unknown table: %r" % (table,))
    return schema, table


def _ident(schema, table):
    """Quoted schema.table reference. The ONLY way dynamic relation
    names reach SQL — never f-strings."""
    return sql.SQL("{}.{}").format(sql.Identifier(schema),
                                   sql.Identifier(table))


def table_metadata(dbname, schema, table):
    """Columns + row-count estimate + on-disk size (DB-05).

    The estimate comes from pg_class.reltuples ("~N (estimate)" in the
    UI) — never a full count(*). The size lookup is fully parameterized
    through pg_class/pg_namespace (no regclass string games), so exotic
    relation names resolve exactly.
    """
    schema, table = checked_relation(dbname, schema, table)
    with _connect(dbname) as conn:
        col_rows = conn.execute(
            "SELECT column_name, data_type, character_maximum_length, "
            "       is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position", (schema, table)).fetchall()
        size_row = conn.execute(
            "SELECT c.reltuples::bigint AS estimate, "
            "       pg_size_pretty(pg_total_relation_size(c.oid)) AS size "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, table)).fetchone()
    columns = [{"name": r[0], "type": r[1], "length": r[2],
                "nullable": r[3]} for r in col_rows]
    estimate = size_row[0] if size_row else 0
    size = size_row[1] if size_row else "0 bytes"
    return {"columns": columns, "estimate": estimate, "size": size}


# --------------------------------------------------------------------------
# Row browsing (DB-04, DB-11)
# --------------------------------------------------------------------------

def browse_rows(dbname, schema, table, page, per_page=ROWS_PER_PAGE):
    """(columns, rows) for one page. ORDER BY ctid gives a stable,
    type-agnostic order on any table — no primary key needed."""
    schema, table = checked_relation(dbname, schema, table)
    page = int(page)
    offset = (page - 1) * per_page
    q = sql.SQL("SELECT * FROM {}.{} ORDER BY ctid LIMIT %s OFFSET %s"
                ).format(sql.Identifier(schema), sql.Identifier(table))
    with _connect(dbname) as conn:
        conn.execute("SET LOCAL statement_timeout = '%s'"
                     % STATEMENT_TIMEOUT)
        cur = conn.execute(q, (per_page, offset))
        cols = [d.name for d in cur.description]
        return cols, cur.fetchall()


def display_cell(value):
    """Normalize one cell for HTML display.

    Returns None (template renders the NULL span), or a plain str.
    Escaping stays in the template (Jinja autoescape) — this function
    only normalizes types, never HTML-escapes (no double-escaping).
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def cell_text(value):
    """Plain-text form of one cell for CSV export."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


# --------------------------------------------------------------------------
# Statement classifier (DB-07) — pure function, no DB access
# --------------------------------------------------------------------------

def _strip_noise(text):
    # Remove -- line comments, /* */ blocks, and '...' literals (with ''
    # escapes) so keywords smuggled inside them cannot change the verdict.
    s = re.sub(r"--[^\n]*", " ", text)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"'(?:[^']|'')*'", "''", s)
    return s


def classify_statement(text):
    """Classify one SQL statement: "read" | "write" | "invalid".

    Default-deny: only a leading SELECT/WITH/VALUES/TABLE/EXPLAIN (after
    noise stripping) is a read, minus the known write-shaped exceptions
    (EXPLAIN ANALYZE, data-modifying CTEs, SELECT INTO). Anything else —
    DDL, DML, GRANT, COPY, ... — is "write" and needs write mode.
    Multiple statements are "invalid" outright.
    """
    s = _strip_noise(text or "").strip()
    if not s:
        return "invalid"
    if ";" in s:
        return "invalid"          # multi-statement: refuse outright
    first = s.split(None, 1)[0].lower()
    if first not in READ_FIRST:
        return "write"            # default-deny
    if first == "explain" and re.search(r"\banalyze\b", s, re.I):
        return "write"            # EXPLAIN ANALYZE really executes
    if first == "with" and re.search(r"\b(insert|update|delete|merge)\b",
                                     s, re.I):
        return "write"            # data-modifying CTE
    if first == "select" and re.search(r"\binto\b", s, re.I):
        return "write"            # SELECT INTO creates a table
    return "read"


# --------------------------------------------------------------------------
# Execution (DB-06, DB-07, DB-08)
# --------------------------------------------------------------------------

def _apply_guards(conn, mode):
    # SET TRANSACTION must be the first statement of the transaction;
    # psycopg3 opens the transaction implicitly on first execute, so this
    # ordering holds. SET LOCAL scopes the timeout to this transaction
    # only — it cannot leak into other requests.
    conn.execute("SET TRANSACTION READ ONLY" if mode == "read"
                 else "SET TRANSACTION READ WRITE")
    conn.execute("SET LOCAL statement_timeout = '%s'" % STATEMENT_TIMEOUT)


def run_sql(dbname, sqltext, allow_write=False):
    """Run one statement. Returns (cols, rows, truncated, mode).

    Read path: up to RESULT_ROW_CAP rows, inside READ ONLY.
    Write path: only with allow_write=True, committed explicitly.
    Raises ValueError (refused), PermissionError (read-only),
    StatementTimeoutError (~30s), NotConfiguredError.
    """
    dbname = checked_dbname(dbname)
    mode = classify_statement(sqltext)
    if mode == "invalid":
        raise ValueError("Refused: empty or multi-statement SQL.")
    if mode == "write" and not allow_write:
        raise PermissionError(
            "Refused: destructive statement while in read-only mode. "
            "Enable write mode for this database (explicit confirmation) "
            "to run it.")
    try:
        with _connect(dbname) as conn:      # autocommit off: implicit txn
            _apply_guards(conn, mode)
            cur = conn.execute(sqltext)     # psycopg3 also rejects
                                            # multi-statement here
            cols = [d.name for d in cur.description] \
                if cur.description else []
            rows = cur.fetchmany(RESULT_ROW_CAP)
            truncated = cur.fetchone() is not None
            if mode == "write":
                conn.commit()
            return cols, rows, truncated, mode
    except psycopg.errors.QueryCanceled as e:
        raise StatementTimeoutError(
            "Query cancelled: exceeded %s statement timeout."
            % STATEMENT_TIMEOUT) from e


def stream_sql_batches(dbname, sqltext, batch_size=EXPORT_BATCH):
    """Yield (cols, rows) batches for CSV export (DB-12).

    Read-only always: export NEVER honors write mode. A named
    server-side cursor keeps memory flat no matter the result size.
    """
    dbname = checked_dbname(dbname)
    if classify_statement(sqltext) != "read":
        raise PermissionError("Export is read-only.")
    try:
        with _connect(dbname) as conn:
            _apply_guards(conn, "read")
            with conn.cursor(name="db_export_cursor") as cur:
                cur.itersize = batch_size
                cur.execute(sqltext)
                cols = [d.name for d in cur.description] \
                    if cur.description else []
                while True:
                    rows = cur.fetchmany(batch_size)
                    if not rows:
                        break
                    yield cols, rows
    except psycopg.errors.QueryCanceled as e:
        raise StatementTimeoutError(
            "Query cancelled: exceeded %s statement timeout."
            % STATEMENT_TIMEOUT) from e
