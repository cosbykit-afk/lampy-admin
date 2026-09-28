"""R Theory website module for Lampy Administration App.

Provides:
- Website deployment (sync to /var/www/html/r-theory)
- HTML parsing (extract normalized text chunks)
- Vectorization (Ollama nomic-embed-text, 768-dim)
- Semantic search over the vectorized content

All operations are admin-only and keyboard-accessible.
"""
import os
import re
import html
import json
import unicodedata
import urllib.request
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# HTML Parser
# ---------------------------------------------------------------------------

BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "blockquote", "td", "th", "caption"}
SKIP_TAGS = {"nav", "style", "script", "footer", "form", "button", "noscript"}
VOID_TAGS = {"input", "br", "hr", "img", "meta", "link", "area", "base", "col",
             "embed", "source", "track", "wbr"}
SKIP_CLASSES = {"sitenav"}


class RTheoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self._skip_stack = []
        self._current_tag = None
        self._current_text = []
        self._heading_path = []
        self.title = ""

    def _in_skip(self):
        return len(self._skip_stack) > 0

    def _has_skip_class(self, attrs):
        for name, value in attrs:
            if name == "class" and value:
                if any(c in SKIP_CLASSES for c in value.split()):
                    return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        if (tag in SKIP_TAGS) or self._has_skip_class(attrs):
            self._skip_stack.append(tag)
            return
        if self._in_skip():
            return
        if tag == "title":
            self._current_tag = "title"
            self._current_text = []
        elif tag in BLOCK_TAGS:
            self._current_tag = tag
            self._current_text = []

    def handle_endtag(self, tag):
        if self._in_skip():
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return
        if tag == "title" and self._current_tag == "title":
            self.title = self._flush()
            self._current_tag = None
        elif tag in BLOCK_TAGS and self._current_tag == tag:
            text = self._flush()
            if text:
                if tag in ("h1", "h2", "h3", "h4"):
                    level = int(tag[1])
                    while self._heading_path and self._heading_path[-1][0] >= level:
                        self._heading_path.pop()
                    self._heading_path.append((level, text))
                path = " > ".join(h[1] for h in self._heading_path)
                self.blocks.append((tag, text, path))
            self._current_tag = None

    def handle_data(self, data):
        if self._in_skip():
            return
        if self._current_tag:
            self._current_text.append(data)

    def _flush(self):
        raw = "".join(self._current_text)
        return normalize_text(raw)


def normalize_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_file(filepath):
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    parser = RTheoryParser()
    parser.feed(content)
    return parser.title, parser.blocks


def chunk_blocks(title, blocks, source_path, max_chars=2000):
    chunks = []
    current, current_len, current_path = [], 0, ""

    def flush():
        nonlocal current, current_len
        if current:
            text = "\n\n".join(current)
            chunks.append({
                "source": source_path,
                "title": title,
                "heading_path": current_path,
                "text": text,
                "char_count": len(text),
            })
            current, current_len = [], 0

    for tag, text, path in blocks:
        if tag in ("h1", "h2") and current:
            flush()
        current_path = path
        if current and current_len + len(text) + 2 > max_chars:
            flush()
        current.append(text)
        current_len += len(text) + 2
    flush()
    return chunks


def parse_site(site_dir):
    all_chunks = []
    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if d != "graphs"]
        for fn in sorted(files):
            if not fn.endswith(".html"):
                continue
            filepath = os.path.join(root, fn)
            rel = os.path.relpath(filepath, site_dir)
            try:
                title, blocks = parse_file(filepath)
                all_chunks.extend(chunk_blocks(title, blocks, rel))
            except Exception:
                pass
    return all_chunks


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def ensure_tables(db):
    """Create rtheory_chunks table. Returns True if pgvector available."""
    try:
        db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        has_vector = True
    except Exception:
        has_vector = False
    if has_vector:
        db.execute("""
            CREATE TABLE IF NOT EXISTS rtheory_chunks (
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                heading_path TEXT,
                text TEXT NOT NULL,
                char_count INTEGER,
                embedding vector(768),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    else:
        db.execute("""
            CREATE TABLE IF NOT EXISTS rtheory_chunks (
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                heading_path TEXT,
                text TEXT NOT NULL,
                char_count INTEGER,
                embedding TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    return has_vector


def get_stats(db):
    """Return (total_chunks, vectorized_chunks, total_chars)."""
    rows = db.query(
        "SELECT COUNT(*), COUNT(embedding), COALESCE(SUM(char_count),0) "
        "FROM rtheory_chunks")
    if rows:
        return rows[0]["count"], rows[0]["count_1"], rows[0]["coalesce"]
    return 0, 0, 0


def store_chunks(db, chunks, embed_fn=None, progress_cb=None):
    """Store chunks in DB, optionally vectorizing each."""
    db.execute("DELETE FROM rtheory_chunks")
    for i, chunk in enumerate(chunks):
        vec_str = None
        if embed_fn:
            try:
                vec = embed_fn(chunk["text"][:8000])
                if vec:
                    vec_str = "[" + ",".join(map(str, vec)) + "]"
            except Exception:
                pass
        db.execute(
            "INSERT INTO rtheory_chunks "
            "(source, title, heading_path, text, char_count, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (chunk["source"], chunk["title"], chunk["heading_path"],
             chunk["text"], chunk["char_count"], vec_str))
        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, len(chunks))
    return len(chunks)


def semantic_search(db, query_vector, limit=10):
    """Find most similar chunks by cosine distance."""
    vec_str = "[" + ",".join(map(str, query_vector)) + "]"
    return db.query(
        "SELECT id, source, title, heading_path, "
        "LEFT(text, 300) AS snippet, "
        "1 - (embedding <=> %s::vector) AS similarity "
        "FROM rtheory_chunks "
        "WHERE embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector "
        "LIMIT %s",
        (vec_str, vec_str, limit))


# ---------------------------------------------------------------------------
# Ollama embeddings
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "nomic-embed-text"


def embed_text(text, model=EMBED_MODEL):
    req = urllib.request.Request(
        OLLAMA_URL + "/api/embeddings",
        data=json.dumps({"model": model, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("embedding", [])


def check_embed_model():
    """Return True if the embedding model is available."""
    try:
        with urllib.request.urlopen(
                OLLAMA_URL + "/api/tags", timeout=10) as r:
            models = json.loads(r.read()).get("models", [])
            return any(EMBED_MODEL in m.get("name", "") for m in models)
    except Exception:
        return False
