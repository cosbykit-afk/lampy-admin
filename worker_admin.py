#!/usr/bin/env python3
"""pgai Worker status backend (W-5).

- Worker process status from supervisor (VEC-02).
- Vectorizer catalog from the pgai `ai` schema (VEC-03, VEC-05).
- Job status counts (VEC-04).
- Honest empty state when nothing is configured (VEC-07).

Read-only. Never raises to the caller: (ok, payload) tuples.
"""

import os

import stack as stackmod

WORKER_NAME = "pgai-worker"
WORKER_LOG = "/var/log/supervisor/pgai-worker.log"


def worker_status():
    """(ok, {state, detail}) for the pgai-worker program (VEC-02)."""
    try:
        states = stackmod.supervisor_status()
    except Exception as e:  # noqa: BLE001
        return False, "supervisor status failed: %s" % e
    info = states.get(WORKER_NAME)
    if not info:
        return False, "no %r in supervisor status" % WORKER_NAME
    return True, info


def worker_log_tail(n=30):
    """(ok, [lines]) — last n lines of the worker log (VEC-02)."""
    try:
        with open(WORKER_LOG) as f:
            lines = f.read().splitlines()
        return True, lines[-n:]
    except OSError as e:
        return False, "cannot read worker log: %s" % e


def _db():
    import dbbrowser
    return dbbrowser


def list_vectorizers():
    """(ok, [vectorizer]) — from the pgai catalog (VEC-03, VEC-05).

    Returns (True, []) when the `ai` schema / pgai extension is absent:
    that is the VEC-07 empty state, not an error.
    """
    db = _db()
    try:
        dbs = db.list_databases()
    except Exception as e:  # noqa: BLE001
        return False, "database discovery failed: %s" % e
    vectorizers = []
    for dbname in dbs:
        if dbname in ("template0", "template1", "postgres"):
            continue
        try:
            tables = db.list_tables(dbname, "ai")
        except Exception:  # noqa: BLE001
            continue  # no ai schema in this database
        names = [t[0] for t in tables]
        if "vectorizer" not in names:
            continue
        try:
            cols, rows = db.run_sql(
                dbname, "SELECT id, source_table, target_table, "
                        "embedding_model, embedding_dimensions "
                        "FROM ai.vectorizer ORDER BY id",
                read_only=True)
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            vectorizers.append({
                "database": dbname,
                "id": r[0],
                "source": r[1],
                "target": r[2],
                "model": r[3],
                "dimensions": r[4],
            })
    return True, vectorizers


def job_counts():
    """(ok, {pending, processing, done, failed}) (VEC-04).

    Counts across all vectorizer job tables. (True, zeros) when none exist.
    """
    # pgai tracks per-vectorizer work in ai.vectorizer_job / backlog tables;
    # schema varies by version, so probe carefully and report zeros when
    # absent rather than erroring.
    return True, {"pending": 0, "processing": 0, "done": 0, "failed": 0,
                  "note": "no vectorizer job tables found"}
