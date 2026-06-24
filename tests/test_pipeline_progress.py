"""Tests for Pipeline on_stage callback threading through process() / run_until()
(plan: realtime-job-progress U1, test-first).
"""

from __future__ import annotations

from pathlib import Path

from lcp import pipeline as pl
from lcp.adapters.crawler.base import STATUS_CRAWLED, RawJobBundle, SourceSpec
from lcp.adapters.crawler.bundle import build_manifest
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config
from lcp.core.models import SourceType
from lcp.core.rules.risk_rules import RiskInput
from lcp.core.state import JobState

TS = "2026-06-24T00:00:00Z"

CLEAN_SOURCE = (
    "華山文創園區本週末舉辦美食市集。\n"
    "現場有上百個攤位提供各式小吃與飲料。\n"
    "主辦單位預估將吸引大量人潮。"
)


class FakeCrawler:
    def __init__(self, source_text: str = CLEAN_SOURCE):
        self.source_text = source_text

    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        raw = spec.job_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text(self.source_text, encoding="utf-8")
        manifest = build_manifest(
            job_id=spec.job_id,
            source_type=SourceType.LOCAL_DIR,
            source_domain=None,
            fetched_at=None,
            assets=[],
            source_html=None,
            source_text=self.source_text,
            crawl_status=STATUS_CRAWLED,
        )
        from lcp.adapters.storage.manifest import write_manifest
        write_manifest(spec.job_dir, manifest, create_only=True)
        return RawJobBundle(
            job_id=spec.job_id,
            raw_dir=raw,
            manifest=manifest,
            job_status=STATUS_CRAWLED,
        )


def _spec(store: JobStore, job_id: str) -> SourceSpec:
    return SourceSpec(
        job_id=job_id,
        source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir(job_id),
        local_dir=Path("/unused"),
    )


def _pipeline(store: JobStore, audit: AuditLog, *, source: str = CLEAN_SOURCE) -> pl.Pipeline:
    return pl.Pipeline(Config(), store, audit, dry_run=True, crawler=FakeCrawler(source))


def _crawled_job(store: JobStore, audit: AuditLog, job_id: str = "j") -> pl.Pipeline:
    p = _pipeline(store, audit)
    p.stage1(_spec(store, job_id), ts=TS)
    assert store.get_job(job_id).state is JobState.CRAWLED
    return p


# ---------------------------------------------------------------------------
# process() on_stage: risk parks early → callback sees ["risk"] only
# ---------------------------------------------------------------------------


def test_process_on_stage_fires_at_risk_block(tmp_path):
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    p = _crawled_job(store, audit, "j")

    stages: list[str] = []
    p.process(
        "j",
        ts=TS,
        risk_input=RiskInput(title="未成年色情兒童", body="未成年色情內容"),
        on_stage=stages.append,
    )
    # Risk parks the job — callback fires once for "risk", never for "media"/"dedup"
    assert stages == ["risk"]


# ---------------------------------------------------------------------------
# process() on_stage: dedup parks → callback sees ["risk", "media", "dedup"]
# ---------------------------------------------------------------------------


def test_process_on_stage_fires_through_dedup_stop(tmp_path):
    """No site_index.jsonl → dedup gate parks at NEEDS_HUMAN_REVIEW."""
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    p = _crawled_job(store, audit, "j")

    stages: list[str] = []
    res = p.process("j", ts=TS, title="台北華山美食市集週末熱鬧登場", on_stage=stages.append)

    assert res.stopped_at == "dedup"
    # risk and media both pass; dedup parks — callback covers all 3
    assert stages == ["risk", "media", "dedup"]


# ---------------------------------------------------------------------------
# process() on_stage=None: existing callers not broken
# ---------------------------------------------------------------------------


def test_process_without_on_stage_is_backward_compatible(tmp_path):
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    p = _crawled_job(store, audit, "j")
    # No on_stage kwarg → must not raise
    res = p.process("j", ts=TS, title="台北華山美食市集週末熱鬧登場")
    assert res.stopped_at == "dedup"


# ---------------------------------------------------------------------------
# process() on_stage: callback exception is swallowed, gate continues
# ---------------------------------------------------------------------------


def test_process_on_stage_exception_is_swallowed(tmp_path):
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    p = _crawled_job(store, audit, "j")

    def _boom(name: str) -> None:
        raise RuntimeError("callback crash")

    # Must not raise; result should still reflect the gate stop normally
    res = p.process("j", ts=TS, risk_input=RiskInput(title="未成年色情", body="未成年色情"), on_stage=_boom)
    assert res.final_state in (JobState.BLOCKED, JobState.NEEDS_HUMAN_REVIEW)


# ---------------------------------------------------------------------------
# process() on_stage: assemble + lint fire when risk/media/dedup all pass
# (uses an empty site_index so dedup can return UNIQUE; dry_run skips LLM)
# ---------------------------------------------------------------------------


def test_process_on_stage_includes_assemble_and_lint_when_gates_pass(tmp_path):
    """With an empty site index, risk+media+dedup all pass; dry_run skips the
    LLM but still emits 'assemble' and 'lint' callback signals."""
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    # Empty site_index → dedup finds no duplicates → PASSES.
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "site_index.jsonl").write_text("", encoding="utf-8")

    p = _crawled_job(store, audit, "j")
    stages: list[str] = []
    p.process(
        "j",
        ts=TS,
        title="台北華山美食市集週末熱鬧登場",
        site_index_path=tmp_path / "data" / "site_index.jsonl",
        on_stage=stages.append,
    )
    # All 3 uniform gates pass, then assemble and lint signals fire.
    assert stages[:3] == ["risk", "media", "dedup"]
    assert "assemble" in stages
    assert "lint" in stages


# ---------------------------------------------------------------------------
# run_until() on_stage: "crawl" fires FIRST, then the gate chain
# ---------------------------------------------------------------------------


def test_run_until_on_stage_emits_crawl_first(tmp_path):
    """run_until fires 'crawl' before stage1, then the gate chain signals."""
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    p = _pipeline(store, audit)

    stages: list[str] = []
    p.run_until(
        _spec(store, "j"),
        target=pl.TARGET_DRAFT,
        ts=TS,
        title="台北華山美食市集週末熱鬧登場",
        on_stage=stages.append,
    )
    # "crawl" must come before any gate signal
    assert stages[0] == "crawl"
    # At minimum "risk" fires after crawl (dedup parks, so crawl+risk+media+dedup)
    assert "risk" in stages
    assert stages.index("crawl") < stages.index("risk")


def test_run_until_on_stage_none_is_backward_compatible(tmp_path):
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    p = _pipeline(store, audit)
    # No on_stage → must not raise
    res = p.run_until(_spec(store, "j"), target=pl.TARGET_DRAFT, ts=TS, title="t")
    assert res.final_state is not None
