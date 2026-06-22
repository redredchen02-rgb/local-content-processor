"""Output-side sanitization tests (R41) — the root XSS/inert-link fix.

These pin the CRITICAL invariant: any attacker-shapeable string (draft / title /
scraped text / source URL) comes out INERT — escaped, not executable — and a
source URL is plain non-clickable text, never an anchor, never auto-fetched.
Pure string transformation: no I/O, no URL resolution.
"""

from __future__ import annotations

import socket

from lcp.adapters.processor import sanitizer
from lcp.adapters.processor.sanitizer import escape_html, inert_link, sanitize_draft
from lcp.core.draft import Draft, FaqItem, MediaSection, SourceQuote

# --- escape_html: renders inert, not executable ------------------------------


def test_script_tag_is_escaped_inert():
    out = escape_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out


def test_img_onerror_is_escaped_inert():
    out = escape_html("<img src=x onerror=alert(document.cookie)>")
    assert "<img" not in out
    assert "&lt;img" in out
    # the attribute text survives but as inert text, never as live markup
    assert "onerror" in out
    assert ">" not in out  # the closing bracket is escaped


def test_ampersand_escaped_first_no_double_escape():
    out = escape_html("a & b <c>")
    assert out == "a &amp; b &lt;c&gt;"


def test_quotes_escaped():
    out = escape_html("\"double\" 'single'")
    assert "&quot;" in out
    assert "&#x27;" in out
    assert '"' not in out
    assert "'" not in out


def test_escape_html_none_is_empty():
    assert escape_html(None) == ""


def test_escape_html_plain_text_unchanged():
    assert escape_html("普通的中文標題") == "普通的中文標題"


# --- inert_link: plain text, not an anchor, not auto-fetchable ----------------


def test_inert_link_is_not_an_anchor():
    out = inert_link("https://source.example.com/article/123")
    assert "<a" not in out
    assert "href" not in out


def test_inert_link_javascript_scheme_is_escaped():
    out = inert_link("javascript:alert(1)")
    # rendered as inert text; even if dropped into HTML it cannot execute
    assert "<" not in out and ">" not in out
    assert out == "javascript:alert(1)"  # no markup chars to escape here


def test_inert_link_with_markup_is_escaped():
    out = inert_link('https://x.test/"><img src=x onerror=alert(1)>')
    assert "<img" not in out
    assert "&lt;img" in out
    assert "&quot;" in out


def test_inert_link_none_is_empty():
    assert inert_link(None) == ""


def test_inert_link_makes_no_network_request(monkeypatch):
    """inert_link must NEVER resolve/fetch the URL — pure escaping only."""

    def _boom(*a, **k):
        raise AssertionError("inert_link must not resolve/fetch a URL")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "gethostbyname", _boom)
    out = inert_link("http://169.254.169.254/latest/meta-data")  # must not raise
    assert "169.254.169.254" in out


# --- sanitize_draft: whole-draft inert form ----------------------------------


def _hostile_draft() -> Draft:
    return Draft(
        title="<script>steal()</script>頭條",
        intro="<b>引言</b> & more",
        quick_facts=["<img src=x onerror=alert(1)>", "正常事實"],
        event_body="正文 <iframe src=evil></iframe>",
        image_sections=[MediaSection(asset_ref="../../etc/passwd", caption="<svg onload=x>")],
        video_sections=[MediaSection(asset_ref="v/a.mp4", caption="影片<script>")],
        faq=[FaqItem(question="問<script>", answer="答&<>")],
        summary="結尾'單引號'",
        tags=["<b>tag</b>", "正常"],
        keywords=["<script>kw"],
        category="<i>社會</i>",
        quotes=[SourceQuote(text="引用<script>x</script>")],
    )


def test_sanitize_draft_escapes_every_field():
    out = sanitize_draft(
        _hostile_draft(),
        source_urls=["https://evil.test/x", "javascript:alert(1)"],
    )
    # No live markup anywhere in the serialized output.
    import json

    blob = json.dumps(out, ensure_ascii=False)
    assert "<script>" not in blob
    assert "<img" not in blob
    assert "<iframe" not in blob
    assert "<svg" not in blob
    # The escaped entity forms ARE present (proves escaping, not deletion).
    assert "&lt;script&gt;" in blob


def test_sanitize_draft_source_urls_are_inert():
    out = sanitize_draft(_hostile_draft(), source_urls=["https://src.test/a"])
    assert out["source_urls"] == ["https://src.test/a"]  # plain text, no anchor
    assert all("<a" not in u and "href" not in u for u in out["source_urls"])


def test_sanitize_draft_preserves_structure():
    out = sanitize_draft(_hostile_draft())
    assert set(out) >= {
        "title",
        "intro",
        "quick_facts",
        "event_body",
        "image_sections",
        "video_sections",
        "faq",
        "summary",
        "tags",
        "keywords",
        "category",
        "quotes",
        "source_urls",
    }
    assert len(out["faq"]) == 1
    assert out["faq"][0]["question"].startswith("問")


def test_sanitize_draft_is_pure_no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("sanitize_draft must not touch the network")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    out = sanitize_draft(_hostile_draft(), source_urls=["http://10.0.0.1/x"])
    assert out is not None


def test_sanitizer_module_imports_no_url_libraries():
    import sys

    mod = sys.modules[sanitizer.__name__]
    src = open(mod.__file__, encoding="utf-8").read()
    for forbidden in ("import urllib", "import requests", "import socket", "import httpx"):
        assert forbidden not in src, f"{forbidden!r} must not appear in sanitizer"
