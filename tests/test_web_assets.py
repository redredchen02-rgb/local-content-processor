"""Unit 3: source-level guards on the frontend assets.

There is no JS test runner in this repo (GUI logic is tested through `Api` in
Python), so the fetch-proxy bridge is verified by (a) these source assertions,
(b) the Unit 2 real-socket integration tests that exercise the exact endpoints
the proxy calls, and (c) manual Chrome verification. These assertions lock in the
bridge swap and the R41 / no-leftover-pywebview invariants.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
APP_JS = (_ROOT / "src" / "lcp" / "web" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (_ROOT / "src" / "lcp" / "web" / "index.html").read_text(encoding="utf-8")


# --- the fetch-proxy bridge replaced pywebview -----------------------------


def test_no_pywebview_references_remain():
    # The whole pywebview bridge is gone — incl. the old L7 comment and bootstrap.
    assert "pywebview" not in APP_JS
    assert "pywebviewready" not in APP_JS


def test_api_is_a_fetch_proxy():
    assert "new Proxy(" in APP_JS
    assert 'fetch("/api/"' in APP_JS
    assert 'Authorization: "Bearer "' in APP_JS
    assert "JSON.stringify({ args: args })" in APP_JS


def test_bridge_reads_meta_token_and_checks_sentinel():
    assert 'meta[name="lcp-csrf"]' in APP_JS
    # The un-substituted placeholder is detected (sentinel) rather than silently
    # 401-ing every call.
    assert "__LCP_CSRF_TOKEN__" in APP_JS
    assert "tokenReady" in APP_JS


def test_bridge_guards_thenable_probing():
    # Returning a function for "then" would make the proxy look like a promise.
    assert 'name === "then"' in APP_JS


# --- R41: no markup sinks introduced ---------------------------------------


def test_no_markup_sinks():
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in APP_JS, sink


# --- index.html: token placeholder + CSP -----------------------------------


def test_index_has_single_token_placeholder_meta():
    assert INDEX_HTML.count('name="lcp-csrf"') == 1
    assert "__LCP_CSRF_TOKEN__" in INDEX_HTML


def test_index_csp_has_frame_ancestors_and_self_connect():
    assert "frame-ancestors 'none'" in INDEX_HTML
    # connect-src must stay 'self' (the same-origin fetch works) — never widened.
    assert "connect-src 'self'" in INDEX_HTML
    assert "connect-src *" not in INDEX_HTML
