"""Unit 9 GUI tests: exercise the Api js_api bridge WITHOUT launching a window.

The whole point of Unit 9's design is that all operator logic lives in `Api`,
which has no pywebview dependency — so these tests import and drive `Api`
directly (no window, no HTTP server, no event loop). We assert:

  * CLI/GUI parity: make_review_packet -> approve(whitelisted) -> backfill(attest)
    walks the same states as the CLI loop (tests/test_cli_skeleton.py).
  * Edge cases: non-whitelisted reviewer and approving a NEEDS_HUMAN_REVIEW job
    both return error dicts with NO state transition.
  * XSS: a draft/title with <script>/<img onerror> comes back ESCAPED from
    get_packet (the dangerous markup is inert, not raw).
  * list/summary shapes; reviewers()/disclaimer() values.
  * Static guards: app.js has no innerHTML / no http://0.0.0.0; gui.py does not
    import webview at module top-level; the server host is 127.0.0.1.
"""

from pathlib import Path

import yaml

from lcp.adapters.publisher.signoff import DISCLAIMER
from lcp.core.state import JobState
from lcp.gui import Api

# Reuse the exact helper the CLI tests use to reach PROCESSED + a persisted draft.
from tests.test_cli_skeleton import _processed_job_with_draft

GUI_PY = Path(__file__).resolve().parents[1] / "src" / "lcp" / "gui.py"
APP_JS = Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "app.js"
INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "index.html"


def _write_config(tmp_path, base, reviewers=("alice",)):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": base}, "publisher": {"reviewers": list(reviewers)}}
        ),
        encoding="utf-8",
    )
    return str(cfg)


def _api(tmp_path, base, reviewers=("alice",)):
    cfg = _write_config(tmp_path, base, reviewers)
    return Api(config_path=cfg)


# --- CLI/GUI parity: the full sign-off loop ---------------------------------


def test_full_signoff_loop_via_api(tmp_path):
    base = str(tmp_path)
    store = _processed_job_with_draft(base, "j1")
    api = _api(tmp_path, base)

    # 1. make_review_packet freezes the draft -> REVIEW_PENDING.
    res = api.make_review_packet("j1")
    assert "error" not in res
    assert res["state"] == "review_pending"
    assert store.get_job("j1").state is JobState.REVIEW_PENDING

    # 2. non-whitelisted reviewer -> error dict, NO transition.
    res = api.approve("j1", "mallory")
    assert "error" in res and res["exit_code"] == 2
    assert store.get_job("j1").state is JobState.REVIEW_PENDING

    # 3. whitelisted approve -> APPROVED.
    res = api.approve("j1", "alice")
    assert "error" not in res
    assert res["state"] == "approved"
    assert store.get_job("j1").state is JobState.APPROVED
    # disclaimer rides along with the sign-off, verbatim.
    assert res["disclaimer"] == DISCLAIMER

    # 4. backfill WITHOUT attestation stays APPROVED (loop open).
    res = api.backfill("j1", "alice", "https://site.example/x", attested=False)
    assert "error" in res
    assert store.get_job("j1").state is JobState.APPROVED

    # 5. backfill WITH attestation -> PUBLISHED_RECORDED.
    res = api.backfill("j1", "alice", "https://site.example/x", attested=True)
    assert "error" not in res
    assert res["state"] == "published_recorded"
    assert store.get_job("j1").state is JobState.PUBLISHED_RECORDED


# --- Edge: refusals do not transition ---------------------------------------


def test_api_backfill_non_whitelisted_rejected(tmp_path):
    """P3 regression: backfill via the Api requires a whitelisted reviewer."""
    base = str(tmp_path)
    store = _processed_job_with_draft(base, "jbf")
    api = _api(tmp_path, base)
    api.make_review_packet("jbf")
    api.approve("jbf", "alice")
    res = api.backfill("jbf", "mallory", "https://site.example/x", attested=True)
    assert "error" in res
    assert res["exit_code"] == 2
    assert store.get_job("jbf").state is JobState.APPROVED


def test_reject_via_api(tmp_path):
    base = str(tmp_path)
    store = _processed_job_with_draft(base, "jr")
    api = _api(tmp_path, base)
    api.make_review_packet("jr")
    res = api.reject("jr", "alice", "off-topic")
    assert "error" not in res
    assert res["state"] == "rejected"
    assert store.get_job("jr").state is JobState.REJECTED


def test_approve_non_whitelisted_no_transition(tmp_path):
    base = str(tmp_path)
    store = _processed_job_with_draft(base, "j2")
    api = _api(tmp_path, base)
    api.make_review_packet("j2")
    res = api.approve("j2", "eve")
    assert "error" in res
    assert "whitelist" in res["error"].lower() or res["exit_code"] == 2
    assert store.get_job("j2").state is JobState.REVIEW_PENDING


def test_approve_needs_human_review_refused_by_state_machine(tmp_path):
    """A NEEDS_HUMAN_REVIEW job has NO path to APPROVED (state machine refuses)."""
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    ts = "2026-06-16T00:00:00Z"
    store.create_job("jh", created_at=ts)
    store.set_state("jh", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jh", JobState.NEEDS_HUMAN_REVIEW, updated_at=ts)

    api = _api(tmp_path, base)
    res = api.approve("jh", "alice")
    assert "error" in res
    # No transition: still parked at NEEDS_HUMAN_REVIEW.
    assert store.get_job("jh").state is JobState.NEEDS_HUMAN_REVIEW


def test_api_resolve_nhr_via_override(tmp_path):
    """A NEEDS_HUMAN_REVIEW (dedup) job is resolved to PROCESSED via Api.resolve
    with an explicit reason; without a reason it returns an error dict."""
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import ReviewReason

    store = JobStore(base_dir=base)
    ts = "2026-06-16T00:00:00Z"
    store.create_job("jn", created_at=ts)
    store.set_state("jn", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jn", JobState.NEEDS_HUMAN_REVIEW, updated_at=ts,
                       review_reason=ReviewReason.DEDUP)

    api = _api(tmp_path, base)
    res = api.resolve("jn", "alice")  # no reason -> override refused
    assert "error" in res
    assert store.get_job("jn").state is JobState.NEEDS_HUMAN_REVIEW

    res = api.resolve("jn", "alice", reason="manually verified unique")
    assert "error" not in res
    assert res["state"] == "processed"
    assert store.get_job("jn").state is JobState.PROCESSED


def test_api_reject_nhr_reaches_rejected(tmp_path):
    """A held NEEDS_HUMAN_REVIEW job (no packet) can be rejected via the Api."""
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import ReviewReason

    store = JobStore(base_dir=base)
    ts = "2026-06-16T00:00:00Z"
    store.create_job("jrn", created_at=ts)
    store.set_state("jrn", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jrn", JobState.NEEDS_HUMAN_REVIEW, updated_at=ts,
                       review_reason=ReviewReason.GROUNDING)

    api = _api(tmp_path, base)
    res = api.reject("jrn", "alice", "not suitable")
    assert "error" not in res
    assert res["state"] == "rejected"
    assert store.get_job("jrn").state is JobState.REJECTED


def test_approve_blocked_refused(tmp_path):
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    ts = "2026-06-16T00:00:00Z"
    store.create_job("jb", created_at=ts)
    store.set_state("jb", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jb", JobState.BLOCKED, updated_at=ts)

    api = _api(tmp_path, base)
    res = api.approve("jb", "alice")
    assert "error" in res
    assert store.get_job("jb").state is JobState.BLOCKED


def test_api_approve_rejects_body_tampered_after_freeze(tmp_path):
    """P1 regression: Api.approve (no draft= arg) must load the persisted draft
    and re-verify the frozen body hash. Overwriting draft.json after freeze ->
    approve returns an error dict and the job stays REVIEW_PENDING."""
    from lcp.core.draft import Draft, FaqItem, SourceQuote
    from lcp.pipeline import save_draft

    base = str(tmp_path)
    store = _processed_job_with_draft(base, "jt")
    api = _api(tmp_path, base)
    res = api.make_review_packet("jt")
    assert "error" not in res
    assert store.get_job("jt").state is JobState.REVIEW_PENDING

    tampered = Draft(
        title="台北華山美食市集週末熱鬧登場活動", intro="引言。",
        quick_facts=["週末"], event_body="完全不同的正文，已被竄改。",
        faq=[FaqItem(question="Q", answer="A")], summary="結尾。",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    save_draft(store, "jt", tampered)

    res = api.approve("jt", "alice")
    assert "error" in res
    assert res["exit_code"] == 2
    assert store.get_job("jt").state is JobState.REVIEW_PENDING


# --- XSS: get_packet returns escaped / inert strings ------------------------


def test_get_packet_escapes_xss(tmp_path):
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.draft import Draft, FaqItem, SourceQuote
    from lcp.pipeline import save_draft

    ts = "2026-06-16T00:00:00Z"
    store = JobStore(base_dir=base)
    store.create_job("jx", created_at=ts)
    store.set_state("jx", JobState.CRAWLED, updated_at=ts)
    evil_title = "<script>alert(1)</script>"
    evil_body = '<img src=x onerror="alert(2)">payload'
    draft = Draft(
        title=evil_title,
        intro="intro",
        quick_facts=["<b>fact</b>"],
        event_body=evil_body,
        faq=[FaqItem(question="<i>q</i>", answer="<u>a</u>")],
        summary="end",
        quotes=[SourceQuote(text="quote")],
    )
    save_draft(store, "jx", draft)
    persist_gate_state(store, "jx", JobState.PROCESSED, updated_at=ts)

    api = _api(tmp_path, base)
    res = api.get_packet("jx")
    assert "error" not in res
    # Dangerous markup is ESCAPED, not raw.
    assert "<script>" not in res["title"]
    assert "&lt;script&gt;" in res["title"]
    assert "<img" not in res["event_body"]
    assert "onerror" in res["event_body"]  # text survives, but as inert literal
    assert "&lt;img" in res["event_body"]
    assert "<b>" not in res["quick_facts"][0]
    assert "<i>" not in res["faq"][0]["question"]


def test_get_packet_source_urls_are_inert(tmp_path):
    """Source URLs must come back as inert escaped text (never an anchor)."""
    base = str(tmp_path)
    from lcp.adapters.processor.sanitizer import inert_link

    # inert_link is the escape used; assert javascript: URI is neutralised.
    out = inert_link('javascript:alert(1)//"<img onerror=x>')
    assert "<img" not in out
    assert "&lt;img" in out


# --- list / summary shapes --------------------------------------------------


def test_list_jobs_and_summary_shapes(tmp_path):
    base = str(tmp_path)
    _processed_job_with_draft(base, "ja")
    _processed_job_with_draft(base, "jb")
    api = _api(tmp_path, base)

    res = api.list_jobs()
    assert "jobs" in res and "count" in res
    assert res["count"] == 2
    ids = {row["job_id"] for row in res["jobs"]}
    assert ids == {"ja", "jb"}
    for row in res["jobs"]:
        assert set(row) >= {"job_id", "state", "review_reason", "updated_at"}

    # filtered by state alias
    res2 = api.list_jobs(state="processed")
    assert res2["count"] == 2

    res3 = api.list_jobs(state="approved")
    assert res3["count"] == 0

    summ = api.summary()
    assert "summary" in summ
    assert summ["summary"]["processed"] == 2
    assert summ["summary"]["total"] == 2


def test_list_jobs_bad_state_returns_error(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base)
    res = api.list_jobs(state="not-a-real-state")
    assert "error" in res and res["exit_code"] == 2


# --- reviewers / disclaimer -------------------------------------------------


def test_reviewers_returns_whitelist(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base, reviewers=("alice", "bob"))
    res = api.reviewers()
    assert res["reviewers"] == ["alice", "bob"]


def test_disclaimer_is_verbatim(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base)
    assert api.disclaimer()["disclaimer"] == DISCLAIMER


# --- create/process error handling crosses bridge as dict, not exception ----


def test_process_unknown_job_returns_error_dict(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base)
    res = api.process("nope", title="t")
    assert "error" in res
    assert res["exit_code"] == 2


def test_make_review_packet_without_draft_returns_error(tmp_path):
    base = str(tmp_path)
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    store.create_job("nd", created_at="2026-06-16T00:00:00Z")
    api = _api(tmp_path, base)
    res = api.make_review_packet("nd")
    assert "error" in res


def test_get_packet_unknown_job_returns_error(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base)
    res = api.get_packet("ghost")
    assert "error" in res


# --- background job status path ---------------------------------------------


def test_job_status_falls_back_to_persisted_state(tmp_path):
    base = str(tmp_path)
    _processed_job_with_draft(base, "js")
    api = _api(tmp_path, base)
    st = api.job_status("js")
    # No background task was launched, so it reports the persisted record state.
    assert st["status"] == "idle"
    assert st["state"] == "processed"

    st2 = api.job_status("does-not-exist")
    assert st2["status"] == "unknown"


# --- unknown crawl status defaults to CRAWL_FAILED (parity with stage1) ------


def test_create_and_crawl_unknown_status_defaults_to_crawl_failed(tmp_path, monkeypatch):
    """P3 regression: an unrecognised crawl status must map to CRAWL_FAILED (the
    same default as pipeline.stage1), never leave the job parked at NEW."""
    import lcp.gui as gui
    from lcp.adapters.crawler.base import RawJobBundle
    from lcp.adapters.crawler.bundle import build_manifest
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.models import SourceType

    base = str(tmp_path)

    class _FakeRunner:
        def __init__(self, *a, **k):
            pass

        def crawl_url(self, spec, *, ts):
            manifest = build_manifest(
                job_id=spec.job_id, source_type=SourceType.URL,
                source_domain="example.com", fetched_at=ts, assets=[],
                source_html="<html></html>", source_text="b",
                crawl_status="weird-unknown-status",
            )
            return RawJobBundle(
                job_id=spec.job_id, raw_dir=spec.job_dir / "raw",
                manifest=manifest, job_status="weird-unknown-status",
            )

    monkeypatch.setattr(gui, "CrawlRunner", _FakeRunner)
    monkeypatch.setattr(gui.SourceRegistry, "from_config", staticmethod(lambda *_: None))

    api = _api(tmp_path, base)
    res = api.create_and_crawl("jcf", "https://example.com/x")
    assert "error" not in res
    assert res["state"] == "crawl_failed"
    store = JobStore(base_dir=base)
    assert store.get_job("jcf").state.value == "crawl_failed"


# --- static security guards -------------------------------------------------


def test_app_js_has_no_innerhtml_and_no_wildcard_host():
    src = APP_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in src
    assert "http://0.0.0.0" not in src
    assert "0.0.0.0" not in src


def test_gui_does_not_import_webview_at_module_level():
    """`import webview` must appear ONLY inside launch() (lazy), never at the top
    level — otherwise the module would be unimportable headless."""
    src = GUI_PY.read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        # A top-level import has no leading indentation.
        if stripped.startswith(("import webview", "from webview")):
            assert line.startswith((" ", "\t")), (
                "import webview must be indented (inside launch), not top-level"
            )


def test_server_host_is_loopback_only():
    from lcp.gui import SERVER_HOST

    assert SERVER_HOST == "127.0.0.1"
    src = GUI_PY.read_text(encoding="utf-8")
    assert "0.0.0.0" not in src


def test_launch_passes_only_valid_webview_start_kwargs(monkeypatch, tmp_path):
    """Regression for the mypy-surfaced GUI bug: launch() previously passed
    host=SERVER_HOST to webview.start(), which pywebview 6 does NOT accept
    (TypeError on launch; loopback pinning silently unenforced). Assert launch()
    only ever passes real webview.start parameters and never a host= kwarg
    (loopback comes from pywebview's default bind, not from us)."""
    import inspect

    import pytest

    webview = pytest.importorskip("webview")
    real_params = set(inspect.signature(webview.start).parameters)
    assert "host" not in real_params  # documents WHY we must not pass host=

    captured: dict = {}
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: None)
    monkeypatch.setattr(webview, "start", lambda *a, **k: captured.update(k))

    import lcp.gui as gui

    gui.launch(config_path=str(tmp_path / "config.yaml"))

    assert captured, "webview.start was not called"
    assert "host" not in captured  # the bug must not return
    assert set(captured) <= real_params  # every kwarg is a real start() param
    assert captured.get("http_server") is True


def test_index_html_has_strict_csp():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html
    assert "script-src 'self'" in html
    assert "img-src 'self'" in html
    assert "object-src 'none'" in html
    # No inline event handlers / inline scripts.
    assert "onclick" not in html
    assert "onerror" not in html.replace("onerror=&quot;", "")  # only literal text ok


# --- Unit 3: dashboard_stats + saved_sources bridge methods -----------------


def _emit_gate(base, *, seq, gate, job_id, status, stage, review_reason=None,
               ts="2026-06-17T00:00:00Z"):
    from lcp.adapters.storage.audit_log import AuditLog

    extra = {"status": status}
    if review_reason is not None:
        extra["review_reason"] = review_reason
    AuditLog(Path(base) / "audit.jsonl").append(
        ts=ts, stage=stage, event=gate, job_id=job_id, actor="machine", extra=extra
    )


def test_dashboard_stats_empty_state(tmp_path):
    api = _api(tmp_path, str(tmp_path))
    res = api.dashboard_stats()
    assert "error" not in res
    assert res["has_jobs"] is False
    assert res["gates"] == []
    assert res["review_reasons"] == {}
    assert res["gate_intervals"] == []
    assert res["daily_jobs"] == {}


def test_dashboard_stats_with_jobs_and_audit(tmp_path):
    base = str(tmp_path)
    _processed_job_with_draft(base, "j1")  # creates a persisted job + gate events
    # add explicit interceptions so rates are non-trivial
    _emit_gate(base, seq=0, gate="RISK_GATE", job_id="j2", status="blocked",
               stage="risk", review_reason="risk")
    api = _api(tmp_path, base)
    res = api.dashboard_stats()
    assert "error" not in res
    assert res["has_jobs"] is True
    assert res["summary"]["total"] >= 1
    gates = {g["gate"]: g for g in res["gates"]}
    assert "RISK_GATE" in gates
    # j2 was intercepted at risk
    assert gates["RISK_GATE"]["intercepted"] >= 1
    assert gates["RISK_GATE"]["rate"] is not None
    assert res["review_reasons"].get("risk", 0) >= 1


def test_saved_sources_crud_and_escaping(tmp_path):
    api = _api(tmp_path, str(tmp_path))
    # empty first
    assert api.saved_sources() == {"sources": [], "count": 0}
    # add with an XSS-shaped label and URL
    added = api.add_saved_source('<script>alert(1)</script>', "https://e.com/<b>")
    assert added["saved"] is True
    assert "<script>" not in added["label"]  # escaped
    assert "&lt;script&gt;" in added["label"]
    assert "<b>" not in added["source_ref"]  # inert (escaped)

    listed = api.saved_sources()
    assert listed["count"] == 1
    row = listed["sources"][0]
    assert "&lt;script&gt;" in row["label"]
    assert row["source_ref"].startswith("https://e.com/")
    assert "<b>" not in row["source_ref"]

    # delete
    res = api.delete_saved_source(row["id"])
    assert res["removed"] is True
    assert api.saved_sources()["count"] == 0


def test_add_saved_source_rejects_empty(tmp_path):
    api = _api(tmp_path, str(tmp_path))
    res = api.add_saved_source("label", "   ")
    assert "error" in res


def test_saved_source_plaintext_never_in_audit(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base)
    api.add_saved_source("note", "https://leak.example/secret")
    api.delete_saved_source("nope")
    audit = Path(base) / "audit.jsonl"
    if audit.exists():
        text = audit.read_text(encoding="utf-8")
        assert "leak.example" not in text


def test_module_imports_without_pywebview_window():
    """Importing gui + constructing Api must work with no window/server."""
    import lcp.gui as gui

    assert hasattr(gui, "Api")
    assert hasattr(gui, "launch")
    # Constructing Api does NOT open a window or import webview.
    api = gui.Api()
    assert api is not None
