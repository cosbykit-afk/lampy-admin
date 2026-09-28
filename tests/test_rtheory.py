"""Tests for the R Theory module (parser, chunking, normalization)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtheory import (RTheoryParser, normalize_text, parse_site,
                     chunk_blocks, parse_file)


SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>Test Page</title>
<style>body { color: red; }</style>
</head><body>
<nav class="sitenav"><a href="/">Home</a></nav>
<h1>Main Title</h1>
<p>First paragraph with <b>bold</b> text.</p>
<h2>Section One</h2>
<p>Second paragraph &amp; entities.</p>
<footer>Footer content</footer>
</body></html>"""


def _write_sample(tmp_path):
    p = tmp_path / "index.html"
    p.write_text(SAMPLE_HTML)
    return str(p)


def test_normalize_text():
    assert normalize_text("  hello   world  ") == "hello world"
    assert normalize_text("a &amp; b") == "a & b"
    assert normalize_text("") == ""


def test_parser_skips_nav_and_footer(tmp_path):
    path = _write_sample(tmp_path)
    title, blocks = parse_file(path)
    assert title == "Test Page"
    texts = [b[1] for b in blocks]
    # Nav and footer content excluded
    assert not any("Home" in t for t in texts)
    assert not any("Footer" in t for t in texts)
    # Real content present
    assert any("Main Title" in t for t in texts)
    assert any("First paragraph" in t for t in texts)


def test_parser_heading_path(tmp_path):
    path = _write_sample(tmp_path)
    _, blocks = parse_file(path)
    # The h2's path should include the h1
    h2_blocks = [b for b in blocks if b[0] == "h2"]
    assert h2_blocks
    assert "Main Title" in h2_blocks[0][2]


def test_chunk_blocks():
    blocks = [("h1", "Title", "Title"),
              ("p", "Para one.", "Title"),
              ("h2", "Sec", "Title > Sec"),
              ("p", "Para two.", "Title > Sec")]
    chunks = chunk_blocks("T", blocks, "index.html", max_chars=2000)
    # h1/h2 boundary splits into 2 chunks
    assert len(chunks) == 2
    assert chunks[0]["source"] == "index.html"


def test_parse_site(tmp_path):
    sub = tmp_path / "book0"
    sub.mkdir()
    (sub / "index.html").write_text(SAMPLE_HTML)
    (tmp_path / "index.html").write_text(SAMPLE_HTML)
    chunks = parse_site(str(tmp_path))
    assert len(chunks) > 0
    sources = {c["source"] for c in chunks}
    assert "index.html" in sources
    assert "book0/index.html" in sources
