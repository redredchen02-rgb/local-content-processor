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


# --- U1: auto job-id suggestion (app.js) ------------------------------------


def test_suggest_job_id_function_present():
    assert "_suggestJobId" in APP_JS
    assert "_jobIdAutofilled" in APP_JS
    assert "slice(0, 40 - suffix.length)" in APP_JS  # base truncated, suffix always preserved
    assert 'catch (e) { return ""; }' in APP_JS  # error fallback returns empty string
    assert "getUTCFullYear" in APP_JS  # UTC date aligns with CLI _auto_job_id (UTC via _now())


def test_url_input_triggers_suggest_job_id():
    # The URL input event listener calls _suggestJobId.
    assert "create-url" in APP_JS
    assert "_suggestJobId(this.value.trim())" in APP_JS


# --- U2: one-shot quick-mode (app.js + index.html) --------------------------


def test_run_until_draft_async_called_in_quick_mode():
    assert "run_until_draft_async" in APP_JS
    assert "create-quick-mode" in APP_JS
    assert 'enterProgress(jobId, quickMode ? "run" : "crawl")' in APP_JS


def test_stage_label_has_run_case():
    assert 'kind === "run"' in APP_JS


def test_index_has_quick_mode_checkbox():
    assert 'id="create-quick-mode"' in INDEX_HTML


# --- U3: batch-process button (app.js) --------------------------------------


def test_batch_process_button_in_inbox():
    assert "全部处理" in APP_JS
    assert "crawled_warn" in APP_JS  # filter includes crawled_warn
    # Fan-out must call process_async per job, not an abstracted batch endpoint.
    assert "crawledRows.forEach(function (j) { a.process_async(j.job_id" in APP_JS
    assert "if (crawledRows.length)" in APP_JS  # button absent when no crawled jobs


# --- U4: banner CTA hint (app.js) -------------------------------------------


def test_banner_cta_hint_rendered_for_actionable_states():
    assert "banner-cta-hint" in APP_JS
    assert "见下方行动" in APP_JS
    assert "crawled_warn" in APP_JS  # included in ctaStates
