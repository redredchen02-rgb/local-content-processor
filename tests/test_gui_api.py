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
    persist_gate_state(
        store, "jn", JobState.NEEDS_HUMAN_REVIEW, updated_at=ts, review_reason=ReviewReason.DEDUP
    )

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
    persist_gate_state(
        store,
        "jrn",
        JobState.NEEDS_HUMAN_REVIEW,
        updated_at=ts,
        review_reason=ReviewReason.GROUNDING,
    )

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


def test_gui_blocked_recovery_requires_override(tmp_path):
    """U8 GUI parity: a BLOCKED supersede via the bridge is REFUSED without the
    override gesture (the plain supersedeRow path), and SUCCEEDS with it (the
    dedicated redline dialog passes redline_override=True)."""
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    ts = "2026-06-16T00:00:00Z"
    store.create_job("jb", created_at=ts)
    store.set_state("jb", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jb", JobState.BLOCKED, updated_at=ts)

    api = _api(tmp_path, base)
    # plain supersede (no override) -> refused, state unchanged.
    refused = api.supersede("jb")
    assert "error" in refused
    assert store.get_job("jb").state is JobState.BLOCKED
    # dedicated redline dialog passes redline_override=True -> recovered.
    ok = api.supersede("jb", None, True)
    assert ok["state"] == JobState.SUPERSEDED.value
    assert store.get_job("jb").state is JobState.SUPERSEDED


def test_gui_duplicate_recovery_is_single_step(tmp_path):
    """U8 GUI parity: a false-terminal DUPLICATE recovers via the ordinary
    single-step supersede (no override needed)."""
    base = str(tmp_path)
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    ts = "2026-06-16T00:00:00Z"
    store.create_job("jd", created_at=ts)
    store.set_state("jd", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jd", JobState.DUPLICATE, updated_at=ts)

    api = _api(tmp_path, base)
    ok = api.supersede("jd")
    assert ok["state"] == JobState.SUPERSEDED.value
    assert store.get_job("jd").state is JobState.SUPERSEDED


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
        title="台北華山美食市集週末熱鬧登場活動",
        intro="引言。",
        quick_facts=["週末"],
        event_body="完全不同的正文，已被竄改。",
        faq=[FaqItem(question="Q", answer="A")],
        summary="結尾。",
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


def test_get_job_returns_single_job_shape(tmp_path):
    """get_job() returns the same fields as one list_jobs row, O(1)."""
    base = str(tmp_path)
    _processed_job_with_draft(base, "j1")
    api = _api(tmp_path, base)
    res = api.get_job("j1")
    assert "error" not in res
    assert res["job_id"] == "j1"
    assert res["state"] == "processed"
    assert "review_reason" in res
    assert "updated_at" in res
    assert "interrupted" in res
    assert "interrupt_attempts" in res
    assert "interrupt_exhausted" in res


def test_get_job_unknown_returns_error(tmp_path):
    api = _api(tmp_path, str(tmp_path))
    res = api.get_job("ghost")
    assert "error" in res and res["exit_code"] == 2


def test_list_jobs_surfaces_interrupted_job(tmp_path):
    """CLI/GUI parity (U7): a crash-interrupted job (.processing marker on a
    CRAWLED job) is flagged ``interrupted`` through the GUI worklist, just like the
    CLI `list` command — the marker's consumer exists on BOTH shells."""
    from lcp.adapters.storage.job_store import PROCESSING_MARKER, JobStore
    from lcp.core.state import JobState

    base = str(tmp_path)
    s = JobStore(base_dir=base)
    s.create_job("crashed", created_at="2026-06-18T00:00:00Z")
    s.set_state("crashed", JobState.CRAWLED, updated_at="2026-06-18T00:00:00Z")
    # A stale marker a hard crash left behind: owned by a now-DEAD pid, so the
    # in-process reconcile() (which runs under this live test pid) treats it as a
    # crash leftover rather than its own in-flight work (bug_001).
    (s.job_dir("crashed") / PROCESSING_MARKER).write_text("2000000000", encoding="utf-8")

    api = _api(tmp_path, base)
    res = api.list_jobs()
    row = next(r for r in res["jobs"] if r["job_id"] == "crashed")
    assert row["interrupted"] is True
    # reconcile is a pure read: attempts reflects the process-bumped crash counter
    # (0 — no retry yet), NOT a view count.
    assert row["interrupt_attempts"] == 0
    assert row["interrupt_exhausted"] is False


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
    from lcp.adapters.crawler import factory
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
                job_id=spec.job_id,
                source_type=SourceType.URL,
                source_domain="example.com",
                fetched_at=ts,
                assets=[],
                source_html="<html></html>",
                source_text="b",
                crawl_status="weird-unknown-status",
            )
            return RawJobBundle(
                job_id=spec.job_id,
                raw_dir=spec.job_dir / "raw",
                manifest=manifest,
                job_status="weird-unknown-status",
            )

    # build_crawler (U3) constructs the runner/registry now, so patch the factory.
    monkeypatch.setattr(factory, "CrawlRunner", _FakeRunner)
    monkeypatch.setattr(factory.SourceRegistry, "from_config", staticmethod(lambda *_: None))

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


def test_no_webview_import_anywhere_in_src():
    """pywebview is gone — no module under src/lcp may import it. The browser
    webui (lcp.webserver) replaced the desktop window."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "lcp"
    offenders = []
    for py in src_root.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith(("import webview", "from webview")):
                offenders.append(f"{py.name}: {s}")
    assert not offenders, f"webview import(s) must not exist: {offenders}"


def test_server_host_is_loopback_only():
    # SERVER_HOST moved to the transport module (webserver) when launch() left gui.
    from lcp.webserver import SERVER_HOST

    assert SERVER_HOST == "127.0.0.1"
    src = GUI_PY.read_text(encoding="utf-8")
    assert "0.0.0.0" not in src


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


def _emit_gate(
    base, *, seq, gate, job_id, status, stage, review_reason=None, ts="2026-06-17T00:00:00Z"
):
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
    _emit_gate(
        base,
        seq=0,
        gate="RISK_GATE",
        job_id="j2",
        status="blocked",
        stage="risk",
        review_reason="risk",
    )
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
    added = api.add_saved_source("<script>alert(1)</script>", "https://e.com/<b>")
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


def test_dashboard_stats_returns_envelope_on_unreadable_audit(tmp_path):
    # A non-LcpError IO error (here: audit.jsonl is a directory -> read_text
    # raises IsADirectoryError) must come back as a bridge-safe {error} dict,
    # never a raw exception/stack across the bridge.
    base = str(tmp_path)
    (Path(base) / "audit.jsonl").mkdir(parents=True)  # unreadable as a file
    api = _api(tmp_path, base)
    res = api.dashboard_stats()
    assert "error" in res
    assert res["exit_code"] != 0


def test_saved_source_plaintext_never_in_audit(tmp_path):
    base = str(tmp_path)
    api = _api(tmp_path, base)
    api.add_saved_source("note", "https://leak.example/secret")
    api.delete_saved_source("nope")
    audit = Path(base) / "audit.jsonl"
    if audit.exists():
        text = audit.read_text(encoding="utf-8")
        assert "leak.example" not in text


# --- Unit 16: uniform GUI bridge safety -------------------------------------


def _write_validation_report(base, job_id, payload):
    """Write a processed/validation_report.json for cover_report to read."""
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    proc = store.job_dir(job_id) / "processed"
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "validation_report.json").write_text(payload, encoding="utf-8")


def test_cover_report_happy_path_unchanged(tmp_path):
    """U16: a well-formed report still returns the advisory dict, escaped."""
    base = str(tmp_path)
    import json

    _write_validation_report(
        base,
        "jc",
        json.dumps(
            {
                "cover": "cover.jpg",
                "cover_preview": "preview.jpg",
                "cover_advisories": {
                    "geometry": ["<b>too small</b>"],
                    "aesthetic": ["low contrast"],
                },
            }
        ),
    )
    api = _api(tmp_path, base)
    res = api.cover_report("jc")
    assert res["has_report"] is True
    assert res["cover"] == "cover.jpg"
    assert res["cover_preview"] == "preview.jpg"
    # advisory strings are escaped (attacker-shapeable upstream).
    assert "<b>" not in res["geometry"][0]
    assert "&lt;b&gt;" in res["geometry"][0]
    assert res["aesthetic"] == ["low contrast"]


def test_cover_report_no_report_returns_has_report_false(tmp_path):
    """U16: missing report -> {has_report: False}, not an error."""
    base = str(tmp_path)
    api = _api(tmp_path, base)
    res = api.cover_report("absent")
    assert res == {"job_id": "absent", "has_report": False}


def test_cover_report_malformed_json_treated_as_no_report(tmp_path):
    """U16: a malformed (non-JSON) report is a ValueError -> 'no advisory',
    never a raw exception across the bridge."""
    base = str(tmp_path)
    _write_validation_report(base, "jbad", "{ this is not json")
    api = _api(tmp_path, base)
    res = api.cover_report("jbad")  # must NOT raise
    assert res == {"job_id": "jbad", "has_report": False}


def test_cover_report_out_of_band_fault_returns_internal_error(tmp_path, monkeypatch):
    """U16 (the fix): an OUT-OF-BAND exception type (not LcpError/OSError/ValueError)
    must return the 'internal error' dict, not propagate a raw exception. Before
    the fix, cover_report's narrow `except (LcpError, OSError, ValueError)` let
    such a type escape across the bridge."""
    from lcp.gui import Api

    def _boom(self):
        raise RuntimeError("path/secret in here")

    monkeypatch.setattr(Api, "_ctx", _boom)
    base = str(tmp_path)
    api = _api(tmp_path, base)
    res = api.cover_report("j")
    assert res == {"error": "internal error", "exit_code": 5}


def test_cover_report_lcp_error_maps_to_error_dict(tmp_path, monkeypatch):
    """U16: an LcpError still maps to the structured error dict (with its exit
    code), NOT collapsed into the generic 'internal error' — @bridge_safe handles
    it after the inner re-raise."""
    from lcp.core.errors import InputValidationError
    from lcp.gui import Api

    def _boom(self):
        raise InputValidationError("bad input")

    monkeypatch.setattr(Api, "_ctx", _boom)
    base = str(tmp_path)
    api = _api(tmp_path, base)
    res = api.cover_report("j")
    assert "error" in res
    assert res["exit_code"] == 2  # InputValidationError, not the generic 5


# --- Introspection: every public Api method is bridge-safe under a fault -----


def test_every_public_api_method_returns_dict_under_injected_fault(tmp_path, monkeypatch):
    """U16 invariant guard: under an injected fault at the I/O seam, EVERY public
    Api method returns a bridge-safe dict instead of raising — so a future method
    that forgets the @bridge_safe net regresses THIS test, not a webview-stack
    leak in production.

    The injected fault is an LcpError — the exact contract @bridge_safe is built to
    catch and the dominant real failure mode (every adapter raises LcpError on bad
    input). A method that drops the decorator (or replaces it with a too-narrow
    hand-rolled catch like the old cover_report) lets the LcpError escape and fails
    here. The complementary out-of-band-TYPE case is covered for cover_report by
    test_cover_report_out_of_band_fault_returns_internal_error.

    The fault is injected at `_ctx` (the per-call I/O seam every data method goes
    through) AND at `_settings_path` (the seam save_settings touches). The no-_ctx,
    no-collaborator methods (disclaimer; the async variants, which delegate to
    _run_bg and return a 'running' dict) cannot be made to raise, so the invariant
    for them reduces to "returns a dict" — still asserted below."""
    import inspect

    from lcp.core.errors import InputValidationError
    from lcp.gui import Api

    base = str(tmp_path)
    api = _api(tmp_path, base)

    def _ctx_boom(self):
        raise InputValidationError("path/stack/secret must not cross the bridge")

    monkeypatch.setattr(Api, "_ctx", _ctx_boom)
    # save_settings reads _settings_path; make it fail-closed too.
    monkeypatch.setattr(
        Api,
        "_settings_path",
        lambda self: (_ for _ in ()).throw(InputValidationError("boom")),
    )

    # Dummy args by name — enough to reach each method body. Background variants
    # delegate to _run_bg (always returns a 'running' dict) so they need no fault.
    dummy = {
        "job_id": "j",
        "url": "https://e.example/x",
        "directory": str(tmp_path),
        "items_json": "[]",
        "title": "t",
        "reviewer": "alice",
        "reason": "r",
        "new_job_id": None,
        "attested": True,
        "state": None,
        "label": "l",
        "source_ref": "https://e.example/y",
        "source_id": "sid",
        "base_url": "",
        "model": "",
        "api_key": "",
        "dry_run": False,
        "watermark": None,
        "template": None,
        "ai_copy": False,
        "relint": False,
        "redline_override": False,
        "token": "",  # set_tg_token (empty -> status-only, no keyring write)
        "on_stage": None,  # keyword-only internal hook; never sent by the JS bridge
    }

    public = [
        name
        for name, m in inspect.getmembers(Api, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    # Sanity: the introspection actually found the surface we expect to guard.
    assert {"cover_report", "disclaimer", "dashboard_stats", "process"} <= set(public)

    for name in public:
        method = getattr(api, name)
        sig = inspect.signature(method)
        kwargs = {p: dummy[p] for p in sig.parameters if p in dummy}
        missing = [p for p in sig.parameters if p not in dummy]
        assert not missing, f"{name} has un-dummied params {missing}; extend the map"
        result = method(**kwargs)  # MUST NOT raise
        assert isinstance(result, dict), f"{name} returned {type(result)}, not a dict"


def test_module_imports_without_pywebview_window():
    """Importing gui + constructing Api must work with no window/server."""
    import lcp.gui as gui

    assert hasattr(gui, "Api")
    # launch() is gone — the transport lives in lcp.webserver now.
    assert not hasattr(gui, "launch")
    # Constructing Api does NOT open a window or import webview.
    api = gui.Api()
    assert api is not None


# ── ingest report tests ────────────────────────────────────────────────────────


def _make_ingest_report(raw_dir, *, has_body=True, images=2, videos=0, failed=None):
    import json

    (raw_dir / "raw").mkdir(parents=True, exist_ok=True)
    report = {
        "job_id": "j1",
        "has_title": True,
        "has_body": has_body,
        "imported_images": images,
        "imported_videos": videos,
        "failed": failed or [],
        "skipped": 0,
        "truncated_at_max_assets": False,
        "complete": has_body and not failed,
    }
    (raw_dir / "raw" / "ingest_report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )


def test_get_ingest_report_happy_path_returns_counts(tmp_path):
    import yaml

    cfg = tmp_path / "config.yaml"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    cfg.write_text(yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    api = Api(config_path=str(cfg))

    # Create the job and its raw dir.
    job_dir = jobs / "j1"
    job_dir.mkdir()
    _make_ingest_report(job_dir, images=3, videos=1)

    result = api.get_ingest_report("j1")
    assert result["report"] is not None
    assert result["report"]["imported_images"] == 3
    assert result["report"]["imported_videos"] == 1
    assert result["report"]["has_body"] is True


def test_get_ingest_report_partial_has_body_false(tmp_path):
    import yaml

    cfg = tmp_path / "config.yaml"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    cfg.write_text(yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    api = Api(config_path=str(cfg))

    job_dir = jobs / "j1"
    job_dir.mkdir()
    _make_ingest_report(
        job_dir,
        has_body=False,
        failed=[{"name": "bad.jpg", "reason": "corrupt"}],
    )

    result = api.get_ingest_report("j1")
    assert result["report"]["has_body"] is False
    assert len(result["report"]["failed"]) == 1
    assert result["report"]["failed"][0]["name"] == "bad.jpg"


def test_get_ingest_report_absent_returns_none(tmp_path):
    """URL-crawled job has no ingest_report.json → returns None gracefully."""
    import yaml

    cfg = tmp_path / "config.yaml"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    cfg.write_text(yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    api = Api(config_path=str(cfg))

    job_dir = jobs / "j1"
    job_dir.mkdir()  # no raw/ingest_report.json

    result = api.get_ingest_report("j1")
    assert result["report"] is None


def test_get_ingest_report_path_traversal_raises(tmp_path):
    """job_id containing path traversal must raise InputValidationError."""
    import yaml

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    api = Api(config_path=str(cfg))

    result = api.get_ingest_report("../../../etc/passwd")
    # @bridge_safe maps InputValidationError to {"error": ..., "exit_code": 2}
    assert "error" in result and result["exit_code"] == 2


# --- U3: get_source_url API --------------------------------------------------


def _make_source_json(job_dir: Path, url: str, platform: str, title: str = "") -> None:
    import json

    src = job_dir / "source.json"
    src.write_text(json.dumps({"url": url, "platform": platform, "title": title}), encoding="utf-8")
    src.chmod(0o600)


def _setup_url_api(tmp_path):
    import yaml

    cfg = tmp_path / "config.yaml"
    base = str(tmp_path)
    cfg.write_text(yaml.safe_dump({"storage": {"base_dir": base}}), encoding="utf-8")
    from lcp.adapters.storage.job_store import JobStore

    store = JobStore(base_dir=base)
    api = Api(config_path=str(cfg))
    return store, api


def test_get_source_url_returns_url_for_standard_url_job(tmp_path):
    """Standard URL job with platform='url' → found=True and correct URL."""
    store, api = _setup_url_api(tmp_path)
    store.create_job("j1", created_at="2026-01-01T00:00:00Z")
    _make_source_json(store.job_dir("j1"), url="https://example.com/news", platform="url")

    result = api.get_source_url("j1")

    assert result["found"] is True
    assert result["url"] == "https://example.com/news"


def test_get_source_url_gossip_job_returns_not_found(tmp_path):
    """Gossip job (platform='weibo') → found=False (gossip guard)."""
    store, api = _setup_url_api(tmp_path)
    store.create_job("j2", created_at="2026-01-01T00:00:00Z")
    _make_source_json(store.job_dir("j2"), url="https://weibo.com/p/123", platform="weibo", title="吃瓜")

    result = api.get_source_url("j2")

    assert result["found"] is False
    assert result["url"] is None


def test_get_source_url_unknown_job_returns_not_found(tmp_path):
    """Non-existent job_id → found=False (path traversal guard via DB check)."""
    _, api = _setup_url_api(tmp_path)

    result = api.get_source_url("does-not-exist")

    assert result["found"] is False
    assert result["url"] is None


def test_get_source_url_missing_source_json_returns_not_found(tmp_path):
    """Existing job but no source.json → found=False."""
    store, api = _setup_url_api(tmp_path)
    store.create_job("j3", created_at="2026-01-01T00:00:00Z")
    # ensure_job_dir creates the dir but no source.json

    result = api.get_source_url("j3")

    assert result["found"] is False
    assert result["url"] is None


def test_get_source_url_malformed_json_returns_not_found(tmp_path):
    """Unreadable/malformed source.json → found=False (fail-closed)."""
    store, api = _setup_url_api(tmp_path)
    store.create_job("j4", created_at="2026-01-01T00:00:00Z")
    src = store.job_dir("j4") / "source.json"
    src.write_text("not json {{", encoding="utf-8")
    src.chmod(0o600)

    result = api.get_source_url("j4")

    assert result["found"] is False
    assert result["url"] is None


def test_get_source_url_absent_platform_returns_not_found(tmp_path):
    """source.json without 'platform' field → found=False (fail-closed for unknown origin)."""
    import json

    store, api = _setup_url_api(tmp_path)
    store.create_job("j5", created_at="2026-01-01T00:00:00Z")
    src = store.job_dir("j5") / "source.json"
    src.write_text(json.dumps({"url": "https://example.com"}), encoding="utf-8")
    src.chmod(0o600)

    result = api.get_source_url("j5")

    assert result["found"] is False


def test_get_source_url_dangerous_scheme_returns_not_found(tmp_path):
    """javascript:// and data: URLs in source.json are rejected by scheme guard."""
    import json

    store, api = _setup_url_api(tmp_path)
    for i, dangerous_url in enumerate(
        ["javascript://alert(1)", "data:text/html,<script>alert(1)</script>", "ftp://internal.host/file"]
    ):
        job_id = f"jscheme{i}"
        store.create_job(job_id, created_at="2026-01-01T00:00:00Z")
        src = store.job_dir(job_id) / "source.json"
        src.write_text(json.dumps({"url": dangerous_url, "platform": "url"}), encoding="utf-8")
        src.chmod(0o600)

        result = api.get_source_url(job_id)

        assert result["found"] is False, f"scheme guard failed for {dangerous_url}"
        assert result["url"] is None
