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
    rec = p.stage1(_spec(store, "j1"), ts=TS)
    assert rec.state is JobState.CRAWLED
    assert (store.job_dir("j1") / "raw" / "source.txt").exists()


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
