"""Unit tests for dbbrowser.classify_statement and the allowlist gates.

No live database needed: classify_statement is pure, and the allowlist
logic is tested with list_databases stubbed out.

Test IDs follow Database_Tab_Implementation.md section C.1 (T-W1-U1).
"""

from unittest.mock import patch

import pytest

import dbbrowser


# --------------------------------------------------------------------------
# T-W1-U1: classify_statement battery (spec C.1)
# --------------------------------------------------------------------------

CLASSIFY_CASES = [
    # (input, expected, note)
    ("SELECT * FROM t", "read", "plain select"),
    ("  \n -- comment\n WITH x AS (SELECT 1) SELECT * FROM x",
     "read", "comment-prefixed WITH"),
    ("EXPLAIN SELECT * FROM t", "read", "plain explain"),
    ("VALUES (1),(2)", "read", "values"),
    ("TABLE t", "read", "table"),
    ("DROP TABLE t", "write", "drop"),
    ("DELETE FROM t", "write", "delete"),
    ("INSERT INTO t VALUES (1)", "write", "insert"),
    ("UPDATE t SET a=1", "write", "update"),
    ("CREATE TABLE x(a int)", "write", "create"),
    ("ALTER TABLE t ADD c int", "write", "alter"),
    ("TRUNCATE t", "write", "truncate"),
    ("GRANT SELECT ON t TO x", "write", "grant"),
    ("COPY t FROM STDIN", "write", "copy"),
    ("/* sneaky */ DROP TABLE t", "write", "comment cannot smuggle DROP"),
    ("-- hi\nDELETE FROM t", "write", "line comment cannot smuggle DELETE"),
    ("EXPLAIN ANALYZE DELETE FROM t", "write", "explain analyze executes"),
    ("EXPLAIN (ANALYZE) SELECT * FROM t", "write",
     "parenthesized analyze option"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
     "write", "data-modifying CTE"),
    ("WITH u AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM u",
     "write", "update CTE"),
    ("SELECT * INTO newtab FROM t", "write", "select into"),
    ("SELECT 1; DROP TABLE t", "invalid", "multi-statement"),
    ("SELECT 1;--x", "invalid", "multi-statement with comment"),
    ("SELECT 'it''s; not a split'", "read",
     "semicolon inside literal is not a split"),
    ("", "invalid", "empty"),
    ("   \n  ", "invalid", "whitespace only"),
    (";", "invalid", "lone semicolon"),
    ("select", "read", "lowercase"),
    ("  SeLeCt 1", "read", "mixed case + padding"),
    ("SELECT * FROM t WHERE note='do not delete'", "read",
     "keyword inside literal ignored"),
    ("select '/* not a comment */' from t", "read",
     "comment markers inside literal ignored"),
    ("with x as (select 1) select * from x", "read", "lowercase with"),
]


@pytest.mark.parametrize("text,expected,note", CLASSIFY_CASES)
def test_classify_battery(text, expected, note):
    assert dbbrowser.classify_statement(text) == expected, note


def test_classify_none_is_invalid():
    assert dbbrowser.classify_statement(None) == "invalid"


# --------------------------------------------------------------------------
# Allowlist gates: injection strings must raise LookupError
# --------------------------------------------------------------------------

FAKE_DBS = ["forum", "rtheory", "postgres", "gwen_training"]
FAKE_SCHEMAS = {"forum": ["public"], "rtheory": ["public"]}
FAKE_TABLES = {("forum", "public"): [("posts", "BASE TABLE")],
               ("rtheory", "public"): [("notation", "BASE TABLE")]}


def _stub_list_databases():
    return list(FAKE_DBS)


def test_checked_dbname_accepts_known():
    with patch.object(dbbrowser, "list_databases",
                      side_effect=_stub_list_databases):
        assert dbbrowser.checked_dbname("rtheory") == "rtheory"


@pytest.mark.parametrize("evil", [
    "forum'; DROP TABLE posts;--",
    "forum\" OR \"1\"=\"1",
    "../postgres",
    "postgres\n",
    "rtheory ",
    "information_schema",
    "",
    "123",
    "-1",
    "pg_database",
    "f\x00orum",
])
def test_checked_dbname_rejects_injection(evil):
    with patch.object(dbbrowser, "list_databases",
                      side_effect=_stub_list_databases):
        with pytest.raises(LookupError):
            dbbrowser.checked_dbname(evil)


def test_checked_dbname_rejects_unknown_but_wellformed():
    with patch.object(dbbrowser, "list_databases",
                      side_effect=_stub_list_databases):
        with pytest.raises(LookupError):
            dbbrowser.checked_dbname("nosuchdb")


def test_checked_schema_and_relation_allowlist():
    with patch.object(dbbrowser, "list_databases",
                      side_effect=_stub_list_databases), \
         patch.object(dbbrowser, "list_schemas",
                      side_effect=lambda db: FAKE_SCHEMAS[db]), \
         patch.object(dbbrowser, "list_tables",
                      side_effect=lambda db, s: FAKE_TABLES[(db, s)]):
        assert dbbrowser.checked_relation(
            "rtheory", "public", "notation") == ("public", "notation")
        with pytest.raises(LookupError):
            dbbrowser.checked_relation(
                "rtheory", "public", "notation';--")
        with pytest.raises(LookupError):
            dbbrowser.checked_relation(
                "rtheory", "pg_catalog", "pg_class")
        with pytest.raises(LookupError):
            dbbrowser.checked_relation(
                "rtheory", "public", "nosuchtable")


# --------------------------------------------------------------------------
# Display helpers (no DB)
# --------------------------------------------------------------------------

def test_display_cell():
    assert dbbrowser.display_cell(None) is None
    assert dbbrowser.display_cell(b"\x00\xff") == "00ff"
    assert dbbrowser.display_cell("<script>alert(1)</script>") == \
        "<script>alert(1)</script>"  # NOT pre-escaped; template escapes
    assert dbbrowser.display_cell(42) == "42"


def test_cell_text():
    assert dbbrowser.cell_text(None) == ""
    assert dbbrowser.cell_text(b"\x00\xff") == "00ff"
    assert dbbrowser.cell_text("a,b") == "a,b"
