"""Pipeline orchestration tests (Unit 8): run_until, gates, batch summary, list.

Cover: the run --until draft|review flow with an injected fake crawler (proving
the seam), gate-stopping (risk BLOCKED, dedup uncertain without an index), the
counts-by-state batch summary, the pull-style list worklist with state filters,
and dry_run (the LLM is never called; no external mutation; result flagged)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lcp import pipeline as pl
from lcp.adapters.crawler.base import STATUS_CRAWLED, RawJobBundle, SourceSpec
from lcp.adapters.crawler.bundle import build_manifest, sha256_text
from lcp.adapters.processor._persist import persist_gate_state
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config
from lcp.core.errors import InputValidationError
from lcp.core.models import SourceType
from lcp.core.rules.risk_rules import RiskInput
from lcp.core.state import JobState

TS = "2026-06-16T00:00:00Z"

# Neutral content: no redline keywords -> risk gate PASS.
CLEAN_SOURCE = (
    "華山文創園區本週末舉辦美食市集。\n"
    "現場有上百個攤位提供各式小吃與飲料。\n"
    "主辦單位預估將吸引大量人潮。"
)


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


class FakeCrawler:
    """A Crawler-contract impl that writes source.txt + a manifest, no network.

    Proves the seam: Pipeline.run_until drives it exactly like the Scrapy /
    ingest crawlers (plan: a fake works wherever the real impl would)."""

    def __init__(self, source_text: str = CLEAN_SOURCE):
        self.source_text = source_text

    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        raw = spec.job_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text(self.source_text, encoding="utf-8")
        manifest = build_manifest(
            job_id=spec.job_id, source_type=SourceType.LOCAL_DIR,
            source_domain=None, fetched_at=None, assets=[],
            source_html=None, source_text=self.source_text,
            crawl_status=STATUS_CRAWLED,
        )
        from lcp.adapters.storage.manifest import write_manifest

        write_manifest(spec.job_dir, manifest, create_only=True)
        return RawJobBundle(
            job_id=spec.job_id, raw_dir=raw, manifest=manifest,
            job_status=STATUS_CRAWLED,
        )


def _spec(store, job_id):
    return SourceSpec(
        job_id=job_id, source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir(job_id), local_dir=Path("/unused"),
    )


def _pipeline(store, audit, *, dry_run=False, source=CLEAN_SOURCE):
    return pl.Pipeline(
        Config(), store, audit, dry_run=dry_run, crawler=FakeCrawler(source),
    )


# --- Stage 1 via injected fake crawler (seam) --------------------------------


def test_stage1_with_fake_crawler_reaches_crawled(store, audit):
    p = _pipeline(store, audit)
    rec = p.stage1(_spec(store, "j1"), ts=TS).record
    assert rec.state is JobState.CRAWLED
    assert (store.job_dir("j1") / "raw" / "source.txt").exists()


# --- U9: Stage-1 persists (state, hashes) atomically; re-crawl refuses early --


def test_stage1_persists_state_and_hashes_atomically(store, audit):
    """A successful Stage 1 lands the CRAWLED state AND the source hashes
    together (one transaction) — assert both are present on the persisted row."""
    p = _pipeline(store, audit)
    p.stage1(_spec(store, "j1"), ts=TS)
    got = store.get_job("j1")
    assert got.state is JobState.CRAWLED
    # FakeCrawler hands source_text (not html), so source_text_sha256 is set and
    # source_html_sha256 stays None — the state and that hash landed together.
    assert got.source_text_sha256 == sha256_text(CLEAN_SOURCE)
    assert got.source_html_sha256 is None


def test_stage1_recrawl_refused_before_mutation(store, audit):
    """Re-crawling an existing CRAWLED job refuses with a clear
    InputValidationError BEFORE any hash mutation — the persisted (state, hashes)
    from the first crawl is untouched (no partial write)."""
    p = _pipeline(store, audit)
    p.stage1(_spec(store, "j1"), ts=TS)  # first crawl -> CRAWLED + hashes
    before = store.get_job("j1")
    assert before.state is JobState.CRAWLED

    # A second crawl into the SAME job id must refuse; use a DIFFERENT source so a
    # silent clobber would be detectable in the hash.
    p2 = _pipeline(store, audit, source="完全不同的內容，不應該被寫入。")
    with pytest.raises(InputValidationError):
        p2.stage1(_spec(store, "j1"), ts="2026-06-17T00:00:00Z")

    after = store.get_job("j1")
    assert after.state is JobState.CRAWLED  # unchanged
    assert after.source_text_sha256 == sha256_text(CLEAN_SOURCE)  # not clobbered
    assert after.updated_at == before.updated_at  # no mutation at all


def test_stage1_recrawl_refused_for_crawled_warn(store, audit):
    """The refusal covers CRAWLED_WARN too (any already-crawled non-NEW state)."""
    p = _pipeline(store, audit)
    store.create_job("jw", created_at=TS)
    store.set_state("jw", JobState.CRAWLED_WARN, updated_at=TS)
    with pytest.raises(InputValidationError):
        p.stage1(_spec(store, "jw"), ts=TS)


def test_stage1_brand_new_job_creates_and_persists(store, audit):
    """Edge: a brand-new job id (no record yet) still creates + persists normally
    — the re-crawl refusal must not block the legitimate fresh-job path."""
    p = _pipeline(store, audit)
    assert store.get_job("fresh") is None
    rec = p.stage1(_spec(store, "fresh"), ts=TS).record
    assert rec.state is JobState.CRAWLED
    assert store.get_job("fresh").source_text_sha256 == sha256_text(CLEAN_SOURCE)


def test_stage1_recrawl_allowed_for_crawl_failed(store, audit):
    """bug_007: a CRAWL_FAILED job wrote NO bundle (nothing to clobber), and the
    state machine (CRAWL_FAILED -> NEW retry edge) + the GUI 重新抓取 affordance both
    advertise it as retriable. stage1 must allow an in-place re-crawl — reset to NEW
    then land the fresh outcome — NOT dead-end it the way an already-crawled job is
    refused (supersede refuses CRAWL_FAILED too, so a refusal would trap the job)."""
    p = _pipeline(store, audit)
    store.create_job("jcf", created_at=TS)
    store.set_state("jcf", JobState.CRAWL_FAILED, updated_at=TS)

    rec = p.stage1(_spec(store, "jcf"), ts=TS).record

    assert rec.state is JobState.CRAWLED  # re-crawl succeeded in place
    assert (store.job_dir("jcf") / "raw" / "source.txt").exists()
    assert store.get_job("jcf").source_text_sha256 == sha256_text(CLEAN_SOURCE)


# --- dry_run: LLM not called, no external mutation, result flagged ------------


def test_dry_run_does_not_call_llm(store, audit, monkeypatch):
    """With a site index present (dedup unique) the run reaches the assemble
    step; in dry-run the LlmClient must NOT hit the API. We assert the client
    short-circuits (executed=False) and the result is flagged."""
    # Present-but-empty site index so dedup can return unique (HIGH reliability).
    (store.base_dir / "site_index.jsonl").write_text("", encoding="utf-8")

    p = _pipeline(store, audit, dry_run=True)
    p.stage1(_spec(store, "jd"), ts=TS)

    # Guard: a dry-run client must never construct a real openai client.
    import lcp.adapters.llm.client as client_mod

    def _boom(*a, **k):
        raise AssertionError("openai client must not be built in dry-run")

    monkeypatch.setattr(client_mod, "OpenAI", _boom, raising=False)

    res = p.process("jd", ts=TS, title="台北華山美食市集週末熱鬧登場")
    assert res.dry_run is True
    # The draft (if produced) was a not-executed stub.
    if res.draft is not None:
        assert res.draft.executed is False
    assert any("dry-run" in n for n in res.notes)


# --- gate stopping: risk redline -> BLOCKED ----------------------------------


def test_process_stops_at_risk_block(store, audit):
    p = _pipeline(store, audit)
    p.stage1(_spec(store, "jr"), ts=TS)
    # Force a redline by handing the gate a RiskInput a redline keyword matches.
    res = p.process(
        "jr", ts=TS,
        risk_input=RiskInput(title="未成年 18歲以下 兒童 色情", body="未成年色情內容"),
    )
    # Either BLOCKED (redline) or NEEDS_HUMAN_REVIEW (daily/uncertain) — both are
    # gate stops that do NOT reach PROCESSED.
    assert res.final_state in (JobState.BLOCKED, JobState.NEEDS_HUMAN_REVIEW)
    assert res.stopped_at == "risk"
    assert res.final_state is not JobState.PROCESSED


# --- gate stopping: no site index -> dedup uncertain (fail-loud) -------------


def test_process_stops_at_dedup_without_index(store, audit):
    p = _pipeline(store, audit)
    p.stage1(_spec(store, "jdup"), ts=TS)
    # No site_index.jsonl -> reliability LOW -> unique downgraded to uncertain.
    res = p.process("jdup", ts=TS, title="台北華山美食市集週末熱鬧登場")
    assert res.stopped_at == "dedup"
    assert res.final_state is JobState.NEEDS_HUMAN_REVIEW


# --- run_until draft: stops early at a gate, reports resting state ------------


def test_run_until_draft_reports_gate_stop(store, audit):
    p = _pipeline(store, audit)
    res = p.run_until(_spec(store, "ju"), target=pl.TARGET_DRAFT, ts=TS,
                      title="台北華山美食市集週末熱鬧登場")
    # No index -> dedup parks the job; run_until returns that resting state.
    assert res.final_state is JobState.NEEDS_HUMAN_REVIEW
    assert res.target == pl.TARGET_DRAFT


# --- run_until review: a PROCESSED job builds the packet ----------------------


def test_run_until_review_builds_packet_from_processed(store, audit):
    """Drive a job to PROCESSED out-of-band (gates are tested above), then prove
    run_until review's packet step transitions it to REVIEW_PENDING."""
    # Build a CRAWLED job, then directly to PROCESSED via the gate seam.
    store.create_job("jp", created_at=TS)
    store.set_state("jp", JobState.CRAWLED, updated_at=TS)
    (store.job_dir("jp") / "raw").mkdir(parents=True, exist_ok=True)
    (store.job_dir("jp") / "raw" / "source.txt").write_text(CLEAN_SOURCE, encoding="utf-8")
    persist_gate_state(store, "jp", JobState.PROCESSED, updated_at=TS)

    from lcp.adapters.publisher.review_packet import build_review_packet
    from lcp.core.draft import Draft, FaqItem, SourceQuote

    draft = Draft(
        title="台北華山美食市集週末熱鬧登場", intro="引言。",
        quick_facts=["週末"], event_body=CLEAN_SOURCE,
        faq=[FaqItem(question="Q", answer="A")], summary="結尾。",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    packet = build_review_packet(
        job_id="jp", draft=draft, store=store, audit=audit, submitted_at=TS,
    )
    assert store.get_job("jp").state is JobState.REVIEW_PENDING
    assert packet.body_sha256


# --- batch summary: counts-by-state ------------------------------------------


def test_batch_summary_counts_by_state(store, audit):
    # j1 -> CRAWLED, j2 -> NEEDS_HUMAN_REVIEW (dedup), j3 -> BLOCKED.
    p = _pipeline(store, audit)
    p.stage1(_spec(store, "j1"), ts=TS)  # stays CRAWLED

    p.stage1(_spec(store, "j2"), ts=TS)
    p.process("j2", ts=TS, title="台北華山美食市集週末熱鬧登場")  # dedup -> NHR

    store.create_job("j3", created_at=TS)
    store.set_state("j3", JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, "j3", JobState.BLOCKED, updated_at=TS)

    summary = pl.batch_summary(store)
    assert summary[JobState.CRAWLED.value] == 1
    assert summary[JobState.NEEDS_HUMAN_REVIEW.value] == 1
    assert summary[JobState.BLOCKED.value] == 1
    assert summary["total"] == 3
    # PROCESSING is transient and never counted.
    assert JobState.PROCESSING.value not in summary


# --- list worklist: state filters --------------------------------------------


def test_list_jobs_filters_by_state_alias(store, audit):
    p = _pipeline(store, audit)
    p.stage1(_spec(store, "a"), ts=TS)
    p.stage1(_spec(store, "b"), ts=TS)
    p.process("b", ts=TS, title="台北華山美食市集週末熱鬧登場")  # -> NEEDS_HUMAN_REVIEW
    store.create_job("c", created_at=TS)
    store.set_state("c", JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, "c", JobState.DUPLICATE, updated_at=TS)

    needs = pl.list_jobs(store, "needs-review")
    assert {r.job_id for r in needs} == {"b"}

    dups = pl.list_jobs(store, "duplicate")
    assert {r.job_id for r in dups} == {"c"}

    crawled = pl.list_jobs(store, "crawled")
    assert {r.job_id for r in crawled} == {"a"}

    # No filter -> all persisted jobs.
    assert {r.job_id for r in pl.list_jobs(store)} == {"a", "b", "c"}


def test_list_jobs_unknown_state_raises(store, audit):
    from lcp.core.errors import InputValidationError

    with pytest.raises(InputValidationError):
        pl.list_jobs(store, "bogus-state")


def test_resolve_state_aliases():
    assert pl.resolve_state("pending") is JobState.REVIEW_PENDING
    assert pl.resolve_state("published") is JobState.PUBLISHED_RECORDED
    assert pl.resolve_state("blocked") is JobState.BLOCKED


# --- U7: .processing crash-marker reconciliation -----------------------------


# A pid safely above any real pid_max, so os.kill(_, 0) raises ProcessLookupError
# -> _pid_alive() reports it dead. Lets a test forge a marker that looks like a
# HARD-CRASH leftover (owned by a now-dead process), as opposed to mark_processing()
# stamping THIS live process's pid (which reconcile treats as in-flight work).
_DEAD_PID = 2_000_000_000


def _stale_marker(store, job_id):
    """Write a .processing marker owned by a now-DEAD pid: a hard-crash leftover."""
    from lcp.adapters.storage.job_store import PROCESSING_MARKER

    d = store.job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / PROCESSING_MARKER).write_text(str(_DEAD_PID), encoding="utf-8")


def _crashed_mid_stage2(store, job_id, state=JobState.CRAWLED):
    """Simulate a hard crash mid-Stage-2: a resting state + a stale .processing
    marker owned by a dead process (process() clears it in `finally`; a crash never
    reaches it, and the process that set it is gone)."""
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, state, updated_at=TS)
    _stale_marker(store, job_id)


def test_reconcile_detects_interrupted_crawled_job(store, audit):
    """A crash mid-Stage-2 (stale marker, job at CRAWLED) is surfaced as interrupted
    so the operator can explicitly re-process it (not silently auto-transitioned)."""
    p = _pipeline(store, audit)
    _crashed_mid_stage2(store, "j1")

    interrupted = p.reconcile()

    assert [i.job_id for i in interrupted] == ["j1"]
    found = interrupted[0]
    assert found.state is JobState.CRAWLED
    # reconcile is a PURE READ: it reports the process-bumped counter (0 here — no
    # retry has happened yet); it does NOT bump on a worklist view (bug_001).
    assert found.attempts == 0
    assert found.exhausted is False
    # Flagged, NOT auto-transitioned: the job stays at its resting state and the
    # marker stays so a re-process is the operator's deliberate action.
    assert store.get_job("j1").state is JobState.CRAWLED


def test_reconcile_is_a_pure_read_no_bump_no_audit(store, audit):
    """bug_001: viewing the worklist must not mutate state. Repeated reconcile
    passes over the same crashed job never bump the counter and never write an
    INTERRUPTED_DETECTED audit event (that belongs to process(), on the retry)."""
    p = _pipeline(store, audit)
    _crashed_mid_stage2(store, "j1")

    for _ in range(5):
        r = p.reconcile()
        assert r[0].attempts == 0 and r[0].exhausted is False

    assert store.read_interrupt_count("j1") == 0  # never bumped by viewing
    events = [l["event"] for l in audit._read_lines()]
    assert "INTERRUPTED_DETECTED" not in events  # no audit spam from views


def test_reconcile_skips_live_in_flight_marker(store, audit):
    """bug_001: a marker owned by a LIVE process (this process — e.g. a GUI
    background-thread crawl mid-Stage-2 with the job resting at CRAWLED) is in-flight
    work. reconcile must NOT mis-flag it as a crash, and must NOT clear it."""
    p = _pipeline(store, audit)
    store.create_job("j1", created_at=TS)
    store.set_state("j1", JobState.CRAWLED, updated_at=TS)
    store.mark_processing("j1")  # live marker: stamped with THIS process's pid

    assert p.reconcile() == []  # not surfaced as interrupted
    assert store.is_processing("j1")  # live marker left intact
    assert store.read_interrupt_count("j1") == 0


def test_reconcile_ignores_healthy_job_without_marker(store, audit):
    """A healthy CRAWLED job with no marker is untouched (not flagged, no counter)."""
    p = _pipeline(store, audit)
    store.create_job("j1", created_at=TS)
    store.set_state("j1", JobState.CRAWLED, updated_at=TS)

    assert p.reconcile() == []
    assert store.read_interrupt_count("j1") == 0


def test_reconcile_clears_stale_marker_at_terminal_state(store, audit):
    """A crash BETWEEN commit and clear_processing can leave a marker on a terminal
    job (e.g. BLOCKED). Reconciliation must only CLEAR it — never reopen a terminal
    job, never flag it as interrupted (it already came to rest correctly)."""
    p = _pipeline(store, audit)
    store.create_job("j1", created_at=TS)
    store.set_state("j1", JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, "j1", JobState.BLOCKED, updated_at=TS)
    _stale_marker(store, "j1")  # dead-pid marker from a crash after COMMIT

    interrupted = p.reconcile()

    assert interrupted == []  # terminal job is never surfaced as recoverable
    assert store.get_job("j1").state is JobState.BLOCKED  # never reopened
    assert not store.is_processing("j1")  # marker cleared


def test_reconcile_reports_exhausted_when_crash_count_exceeds_cap(store, audit):
    """A DETERMINISTIC crash (same input always crashes) surfaces to a human after N
    real retries. process() bumps the crash counter on each retry; reconcile READS it
    and flags `exhausted` once it exceeds the cap (a real test can't SIGKILL
    mid-process, so the accumulated count is driven the store-level way retries do)."""
    p = _pipeline(store, audit)
    _crashed_mid_stage2(store, "j1")

    for _ in range(3):
        store.bump_interrupt_count("j1")
    assert p.reconcile(max_attempts=3)[0].exhausted is False  # 3 == cap, not over

    store.bump_interrupt_count("j1")  # 4th crash-retry
    r = p.reconcile(max_attempts=3)
    assert r[0].attempts == 4 and r[0].exhausted is True


def test_process_retry_after_crash_bumps_and_clears_counter(store, audit):
    """process() owns the crash counter: a retry that finds a STALE marker bumps it
    and writes ONE INTERRUPTED_DETECTED event; a clean completion then clears it."""
    p = _pipeline(store, audit)
    store.create_job("j1", created_at=TS)
    store.set_state("j1", JobState.CRAWLED, updated_at=TS)
    _stale_marker(store, "j1")  # a prior process() crashed here

    # A redline body parks at BLOCKED with no LLM call -> a clean process() return.
    res = p.process(
        "j1", ts=TS, risk_input=RiskInput(title="某新聞", body="涉及未成年的私密內容"),
    )
    assert res.final_state is JobState.BLOCKED

    events = [l for l in audit._read_lines() if l["event"] == "INTERRUPTED_DETECTED"]
    assert len(events) == 1 and events[0]["extra"]["attempts"] == 1
    # Clean completion resets the loop guard and leaves no marker.
    assert store.read_interrupt_count("j1") == 0
    assert not store.is_processing("j1")


def test_process_first_run_does_not_bump(store, audit):
    """A first process() run (no pre-existing marker) is not a retry: no counter
    bump, no INTERRUPTED_DETECTED event."""
    p = _pipeline(store, audit)
    store.create_job("j1", created_at=TS)
    store.set_state("j1", JobState.CRAWLED, updated_at=TS)

    p.process("j1", ts=TS, risk_input=RiskInput(title="某新聞", body="涉及未成年的私密內容"))

    events = [l["event"] for l in audit._read_lines()]
    assert "INTERRUPTED_DETECTED" not in events
    assert store.read_interrupt_count("j1") == 0


def test_reconcile_via_cli_list_seam(store, audit, tmp_path):
    """Drive reconciliation through the real worklist seam (the CLI `list` command),
    not just the Pipeline leaf — the marker consumer must be reachable end-to-end."""
    from click.testing import CliRunner

    from lcp import cli

    _crashed_mid_stage2(store, "j1")
    base = str(store.base_dir)

    runner = CliRunner()
    res = runner.invoke(
        cli.cli,
        ["--output-dir", base, "--json", "list"],
    )
    assert res.exit_code == 0, res.output
    import json

    payload = json.loads(res.output)
    rows = {r["job_id"]: r for r in payload["jobs"]}
    assert rows["j1"]["interrupted"] is True


# --- draft persistence: process saves the exact draft, review-packet reads it -


def test_process_persists_draft_for_review_packet(store, audit):
    """process must persist the assembled draft so review-packet freezes THAT
    exact draft (no re-assembly, no second LLM call)."""
    (store.base_dir / "site_index.jsonl").write_text("", encoding="utf-8")
    p = _pipeline(store, audit, dry_run=True)
    p.stage1(_spec(store, "jp"), ts=TS)
    p.process("jp", ts=TS, title="台北華山美食市集週末熱鬧登場活動")

    loaded = pl.load_draft(store, "jp")
    assert loaded is not None
    # dry-run stub is persisted with executed=False.
    assert loaded.executed is False


def test_load_draft_missing_returns_none(store):
    assert pl.load_draft(store, "nope") is None


# --- assemble verdict: truncated draft short-circuits to NEEDS_REVISION -------


class _FakeChatClient:
    """Minimal LlmClient stand-in for process(): a fixed ChatResult or a raise.

    `assemble` only needs `.chat(...)` returning a ChatResult and `.model`."""

    model = "fake-model"

    def __init__(self, *, result=None, raises=None):
        self._result = result
        self._raises = raises

    def chat(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self._result


def _clean_index(store):
    # Present-but-empty site index so dedup returns unique (not uncertain).
    (store.base_dir / "site_index.jsonl").write_text("", encoding="utf-8")


def test_process_truncated_draft_short_circuits_needs_revision(store, audit, monkeypatch):
    """P2: finish_reason=length -> assemble produces a NEEDS_REVISION draft;
    process must persist NEEDS_REVISION (carrying the truncation reason) WITHOUT
    running grounding/lint."""
    from lcp.adapters.llm.client import ChatResult

    _clean_index(store)
    truncated = ChatResult(
        text="partial...", finish_reason="length", model="fake-model",
        needs_revision=True, revision_reason="truncated:length", executed=True,
    )
    p = pl.Pipeline(
        Config(), store, audit,
        crawler=FakeCrawler(), llm_client=_FakeChatClient(result=truncated),
    )
    p.stage1(_spec(store, "jt"), ts=TS)

    # Grounding/lint must NOT run -> their audit events must be absent.
    res = p.process("jt", ts=TS, title="台北華山美食市集週末熱鬧登場")
    assert res.final_state is JobState.NEEDS_REVISION
    assert res.stopped_at == "assemble"
    assert store.get_job("jt").state is JobState.NEEDS_REVISION
    assert any("truncated:length" in n for n in res.notes)

    events = {l["event"] for l in audit._read_lines() if l["job_id"] == "jt"}
    assert "GROUNDING_GATE" not in events
    assert "LINT_GATE" not in events
    # No leftover .processing marker.
    assert not store.is_processing("jt")


def test_reprocess_needs_revision_job_via_widened_entry_guard(store, audit):
    """U8: a NEEDS_REVISION job can be re-run in place (NEEDS_REVISION ->
    PROCESSING -> target). The state-machine edge was already live + wired in
    web/lex.js, but pipeline.process's entry guard used to reject NEEDS_REVISION,
    so the edge was unreachable. Widening the guard makes the re-run work."""
    from lcp.adapters.llm.client import ChatResult

    _clean_index(store)
    # First pass: a truncated draft parks the job at NEEDS_REVISION.
    truncated = ChatResult(
        text="partial...", finish_reason="length", model="fake-model",
        needs_revision=True, revision_reason="truncated:length", executed=True,
    )
    p1 = pl.Pipeline(
        Config(), store, audit,
        crawler=FakeCrawler(), llm_client=_FakeChatClient(result=truncated),
    )
    p1.stage1(_spec(store, "jrev"), ts=TS)
    res1 = p1.process("jrev", ts=TS, title="台北華山美食市集週末熱鬧登場")
    assert res1.final_state is JobState.NEEDS_REVISION
    assert store.get_job("jrev").state is JobState.NEEDS_REVISION

    # Re-process the same NEEDS_REVISION job (a dry run avoids needing a healthy
    # LLM) — the widened entry guard must ACCEPT NEEDS_REVISION, not raise.
    p2 = _pipeline(store, audit, dry_run=True)
    res2 = p2.process("jrev", ts=TS, title="台北華山美食市集週末熱鬧登場")
    # It actually entered Stage 2 (a legal resting state was reached), not the
    # entry-guard refusal — and never left a .processing marker.
    assert res2.final_state in (
        JobState.PROCESSED, JobState.NEEDS_HUMAN_REVIEW, JobState.NEEDS_REVISION,
        JobState.BLOCKED, JobState.DUPLICATE,
    )
    assert not store.is_processing("jrev")


# --- LLM 5xx mid-process -> PROCESS_FAILED (retriable), marker cleared --------


def test_process_external_error_lands_process_failed_and_retries(store, audit):
    """P2: an LLM ExternalServiceError mid-process must NOT leave the job at
    CRAWLED — it lands PROCESS_FAILED (retriable) with the marker cleared, and a
    re-run from PROCESS_FAILED works end to end."""
    from lcp.adapters.llm.client import ChatResult
    from lcp.core.errors import ExternalServiceError

    _clean_index(store)
    failing = _FakeChatClient(raises=ExternalServiceError("LLM call failed (503)"))
    p = pl.Pipeline(
        Config(), store, audit, crawler=FakeCrawler(), llm_client=failing,
    )
    p.stage1(_spec(store, "je"), ts=TS)

    res = p.process("je", ts=TS, title="台北華山美食市集週末熱鬧登場")
    assert res.final_state is JobState.PROCESS_FAILED
    assert res.stopped_at == "error"
    assert store.get_job("je").state is JobState.PROCESS_FAILED
    assert not store.is_processing("je")  # marker cleared (try/finally)

    # Retry from PROCESS_FAILED with a now-healthy client -> reaches a normal
    # resting state (here a clean draft passes to PROCESSED).
    from lcp.core.draft import DraftStatus

    ok = ChatResult(
        text="重寫後的完整內文，足夠長度。", finish_reason="stop", model="fake-model",
        needs_revision=False, revision_reason=None, executed=True,
    )
    p2 = pl.Pipeline(
        Config(), store, audit, crawler=FakeCrawler(),
        llm_client=_FakeChatClient(result=ok),
    )
    res2 = p2.process("je", ts=TS, title="台北華山美食市集週末熱鬧登場")
    # The retry runs the gates again (no longer stuck at CRAWLED/PROCESS_FAILED).
    assert res2.final_state in (
        JobState.PROCESSED, JobState.NEEDS_HUMAN_REVIEW, JobState.NEEDS_REVISION,
    )
    assert store.get_job("je").state is not JobState.PROCESS_FAILED


# --- dry_run cannot be bypassed by an injected live client -------------------


def test_pipeline_threads_escape_hatch_from_config(store, audit):
    """U7a: when the pipeline auto-builds the LlmClient, the R40 escape hatch
    (ca_bundle / allow_http_hosts) is sourced from config, not left empty."""
    from lcp.core.config import LlmConfig

    cfg = Config(
        llm=LlmConfig(
            base_url="http://127.0.0.1:8000/v1",
            model="m",
            allowed_hosts=["127.0.0.1"],
            ca_bundle="/etc/pki/private-ca.pem",
            allow_http_hosts=["127.0.0.1"],
        )
    )
    p = pl.Pipeline(cfg, store, audit, dry_run=True)
    assert p.llm_client._ca_bundle == "/etc/pki/private-ca.pem"
    assert p.llm_client._allow_http_hosts == frozenset({"127.0.0.1"})


def test_dry_run_forces_injected_client_to_dry_mode(store, audit):
    """P3 regression: Pipeline(dry_run=True, llm_client=<live>) must NOT call the
    API — the injected client is forced into dry mode."""
    from lcp.adapters.llm.client import LlmClient
    from lcp.core.config import LlmConfig

    _clean_index(store)
    called = {"n": 0}

    class _BoomCompletions:
        def create(self, **kwargs):
            called["n"] += 1
            raise AssertionError("live API must not be called under dry_run")

    class _BoomClient:
        def __init__(self, **kwargs):
            import types as _t
            self.chat = _t.SimpleNamespace(completions=_BoomCompletions())

    cfg = Config(
        llm=LlmConfig(
            base_url="https://llm.example.com/v1", model="m",
            allowed_hosts=["llm.example.com"],
        )
    )
    live = LlmClient(cfg, dry_run=False, client_factory=lambda **k: _BoomClient(**k))

    # Pipeline is dry-run but a LIVE client was injected -> must be forced dry.
    p = pl.Pipeline(cfg, store, audit, dry_run=True,
                    crawler=FakeCrawler(), llm_client=live)
    p.stage1(_spec(store, "jdry"), ts=TS)
    res = p.process("jdry", ts=TS, title="台北華山美食市集週末熱鬧登場")

    assert called["n"] == 0  # API never hit
    assert res.dry_run is True
    if res.draft is not None:
        assert res.draft.executed is False
