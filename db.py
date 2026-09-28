"""Postgres data layer for the Lampy web console.

Reads and writes use the same SQL and validation rules as the forum Flask
app (~/workspace/forum/app.py), so the console mirrors the app exactly:

- title: 1..200 chars, body: 1..20000 chars (schema also enforces
  char_length(body) BETWEEN 1 AND 20000)
- new thread: INSERT thread + INSERT first post + forum_events rows
  ("thread_created", "post_created") in one commit
- reply: locked-thread check first, then INSERT post + "post_created"
  event in one commit
- keyword search: posts.body ILIKE %q% OR threads.title ILIKE %q%,
  newest first, LIMIT 20

Writes only happen through the console's own write paths (new thread /
reply / lock toggle), which the user invokes explicitly.
"""

import os

DB_HOST = os.environ.get("FORUM_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("FORUM_DB_PORT", "5432"))
DB_NAME = os.environ.get("FORUM_DB_NAME", "forum")
DB_USER = os.environ.get("FORUM_DB_USER", "forum")
DB_PASS = os.environ.get("FORUM_DB_PASS", "")

TITLE_MAX = 200
BODY_MAX = 20000
POSTS_PER_PAGE = 20


def _conninfo():
    return (
        "host=%s port=%d dbname=%s user=%s password=%s connect_timeout=5"
        % (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS)
    )


def query(sql, params=()):
    """Read query. Returns list of dicts. Raises on failure."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_conninfo(), row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def write(statements):
    """Run [(sql, params), ...] in one transaction. Raises on failure."""
    import psycopg

    with psycopg.connect(_conninfo()) as conn:
        with conn.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params)
        conn.commit()


def db_ping():
    """True when a real SQL query succeeds (stronger than a TCP probe)."""
    try:
        query("SELECT 1 AS ok")
        return True, ""
    except Exception as e:  # noqa: BLE001 — surfaced to the UI
        return False, "%s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------
# Forum reads
# --------------------------------------------------------------------------

def get_categories():
    return query(
        """SELECT c.id, c.name, c.description,
                  COUNT(DISTINCT t.id) AS thread_count,
                  COUNT(p.id) AS post_count
           FROM categories c
           LEFT JOIN threads t ON t.category_id = c.id
           LEFT JOIN posts p ON p.thread_id = t.id
           GROUP BY c.id ORDER BY c.sort_order, c.name""")


def get_latest_threads(limit=15):
    return query(
        """SELECT t.id, t.title, t.created_at, u.username,
                  c.name AS cat_name,
                  (SELECT COUNT(*) FROM posts p WHERE p.thread_id = t.id)
                    AS post_count,
                  (SELECT MAX(p.created_at) FROM posts p
                    WHERE p.thread_id = t.id) AS last_post_at
           FROM threads t
           JOIN users u ON u.id = t.user_id
           JOIN categories c ON c.id = t.category_id
           ORDER BY last_post_at DESC NULLS LAST LIMIT %s""", (limit,))


def get_category(cat_id):
    rows = query(
        "SELECT id, name, description FROM categories WHERE id = %s",
        (cat_id,))
    if not rows:
        return None, []
    threads = query(
        """SELECT t.id, t.title, t.created_at, t.is_locked, u.username,
                  (SELECT COUNT(*) FROM posts p WHERE p.thread_id = t.id)
                    AS post_count,
                  (SELECT MAX(p.created_at) FROM posts p
                    WHERE p.thread_id = t.id) AS last_post_at
           FROM threads t JOIN users u ON u.id = t.user_id
           WHERE t.category_id = %s
           ORDER BY last_post_at DESC NULLS LAST""", (cat_id,))
    return rows[0], threads


def get_thread(thread_id, page=1):
    rows = query(
        """SELECT t.id, t.title, t.is_locked, t.category_id,
                  c.name AS cat_name, u.username AS author
           FROM threads t
           JOIN categories c ON c.id = t.category_id
           JOIN users u ON u.id = t.user_id
           WHERE t.id = %s""", (thread_id,))
    if not rows:
        return None, [], 1, 1
    total = query(
        "SELECT COUNT(*) AS n FROM posts WHERE thread_id = %s",
        (thread_id,))[0]["n"]
    pages = max(1, -(-total // POSTS_PER_PAGE))
    page = max(1, min(page, pages))
    posts = query(
        """SELECT p.id, p.body, p.created_at, u.username
           FROM posts p JOIN users u ON u.id = p.user_id
           WHERE p.thread_id = %s
           ORDER BY p.id ASC LIMIT %s OFFSET %s""",
        (thread_id, POSTS_PER_PAGE, (page - 1) * POSTS_PER_PAGE))
    return rows[0], posts, page, pages


# --------------------------------------------------------------------------
# Forum writes (mirror app.py validation exactly)
# --------------------------------------------------------------------------

def create_thread(category_id, user_id, title, body):
    """Returns (thread_id, error). error is None on success."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        return None, "Title cannot be empty."
    if len(title) > TITLE_MAX:
        return None, "Title is too long (max 200 characters)."
    if not body:
        return None, "Message cannot be empty."
    if len(body) > BODY_MAX:
        return None, "Message is too long (max 20,000 characters)."
    import psycopg

    with psycopg.connect(_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM categories WHERE id = %s", (category_id,))
            if cur.fetchone() is None:
                return None, "Category not found."
            cur.execute(
                """INSERT INTO threads (category_id, user_id, title)
                   VALUES (%s, %s, %s) RETURNING id""",
                (category_id, user_id, title))
            tid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO posts (thread_id, user_id, body)"
                " VALUES (%s, %s, %s)",
                (tid, user_id, body))
            cur.execute(
                "INSERT INTO forum_events (event_type, user_id, thread_id)"
                " VALUES (%s, %s, %s)",
                ("thread_created", user_id, tid))
            cur.execute(
                "INSERT INTO forum_events (event_type, user_id, thread_id)"
                " VALUES (%s, %s, %s)",
                ("post_created", user_id, tid))
        conn.commit()
    return tid, None


def create_reply(thread_id, user_id, body):
    """Returns error string or None on success."""
    body = (body or "").strip()
    import psycopg

    with psycopg.connect(_conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_locked FROM threads WHERE id = %s", (thread_id,))
            row = cur.fetchone()
            if row is None:
                return "Thread not found."
            if row[0]:
                return "This thread is locked."
            if not body:
                return "Reply cannot be empty."
            if len(body) > BODY_MAX:
                return "Reply is too long (max 20,000 characters)."
            cur.execute(
                "INSERT INTO posts (thread_id, user_id, body)"
                " VALUES (%s, %s, %s)",
                (thread_id, user_id, body))
            cur.execute(
                "INSERT INTO forum_events (event_type, user_id, thread_id)"
                " VALUES (%s, %s, %s)",
                ("post_created", user_id, thread_id))
        conn.commit()
    return None


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def keyword_search(q, limit=20):
    like = "%%%s%%" % q
    return query(
        """SELECT p.id, p.body, p.created_at, u.username,
                  t.id AS thread_id, t.title AS thread_title
           FROM posts p
           JOIN users u ON u.id = p.user_id
           JOIN threads t ON t.id = p.thread_id
           WHERE p.body ILIKE %s OR t.title ILIKE %s
           ORDER BY p.created_at DESC LIMIT %s""",
        (like, like, limit))


def vectorizer_status():
    """Mirror the forum app's pgAI probe. Returns a dict describing what
    was found; the forum app's vector_search() is a stub, so the console
    stays in keyword mode either way and says so."""
    try:
        rows = query("SELECT to_regclass('ai.vectorizer') AS r")
    except Exception as e:  # noqa: BLE001
        return {"mode": "keyword",
                "note": "probe failed: %s: %s" % (type(e).__name__, e)}
    if not rows or rows[0]["r"] is None:
        return {"mode": "keyword",
                "note": "no ai.vectorizer relation — keyword mode"}
    try:
        n = query("SELECT count(*) AS n FROM ai.vectorizer")[0]["n"]
    except Exception as e:  # noqa: BLE001
        return {"mode": "keyword",
                "note": "ai.vectorizer present but unreadable: %s" % e}
    if n > 0:
        return {"mode": "keyword",
                "note": "vectorizer configured (%d row(s)) but the forum "
                        "app's semantic query is a stub — keyword mode, "
                        "same as the app" % n}
    return {"mode": "keyword",
            "note": "ai.vectorizer present but empty — keyword mode"}


# --------------------------------------------------------------------------
# Docs
# --------------------------------------------------------------------------

def get_docs():
    return query(
        "SELECT slug, title, diagram_kind, created_at FROM docs ORDER BY slug")


def get_doc(slug):
    rows = query(
        "SELECT slug, title, body, diagram_kind, image_mime, created_at"
        " FROM docs WHERE slug = %s", (slug,))
    return rows[0] if rows else None


def get_doc_image(slug):
    rows = query(
        "SELECT image, image_mime FROM docs WHERE slug = %s", (slug,))
    if not rows or rows[0]["image"] is None:
        return None, None
    return bytes(rows[0]["image"]), rows[0]["image_mime"]


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def get_metrics(limit=90):
    return query(
        "SELECT day, event_type, SUM(n) AS n FROM forum_daily"
        " GROUP BY day, event_type ORDER BY day DESC LIMIT %s", (limit,))


def get_table_counts():
    out = {}
    for tbl in ("users", "categories", "threads", "posts", "forum_events"):
        out[tbl] = query("SELECT COUNT(*) AS n FROM %s" % tbl)[0]["n"]
    return out


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

def get_users():
    return query(
        "SELECT id, username, email, is_admin, created_at FROM users"
        " ORDER BY id")


def set_thread_locked(thread_id, locked):
    write([("UPDATE threads SET is_locked = %s WHERE id = %s",
            (bool(locked), thread_id))])


def get_threads_for_admin(limit=50):
    return query(
        """SELECT t.id, t.title, t.is_locked, c.name AS cat_name,
                  u.username AS author,
                  (SELECT COUNT(*) FROM posts p WHERE p.thread_id = t.id)
                    AS post_count
           FROM threads t
           JOIN categories c ON c.id = t.category_id
           JOIN users u ON u.id = t.user_id
           ORDER BY t.id DESC LIMIT %s""", (limit,))
