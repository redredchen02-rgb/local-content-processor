"""L12 — E2E recovery paths: supersede, reject, audit chain, batch, and reprocess.

Five coverage gaps from the parallel-optimization-sweep plan (L12):

1. test_blocked_redline_supersede_recovery [high] — drives a REAL REDLINE source
   through the real risk gate to BLOCKED, then supersedes it with redline_override.
   All prior supersede tests seed BLOCKED via persist_gate_state; this test ensures
   RiskCategory-code drift or gate-chain changes would not go undetected.

2. test_reject_from_review_pending [med] — full pipeline flow to REVIEW_PENDING,
   then reject(). Verifies the reject path writes the correct audit event and lands
   the job at REJECTED.

3. test_audit_chain_valid_after_full_flow [med] — verify_chain() returns True after
   a complete ingest → process → review-packet → approve flow. A tampered or
   reordered audit log would break the chain; this test catches it.

4. test_process_batch_real_chain [med] — process_batch() with two CRAWLED jobs drives
   both through the real gate chain independently and emits a BATCH_SUMMARY event.

5. test_needs_revision_reprocess_recovery [med] — a title below the 25-char lint
   minimum parks the job at NEEDS_REVISION; a second process() call with a valid
   title recovers to PROCESSED via the NEEDS_REVISION -> PROCESSING -> PROCESSED
   path (the only in-place recovery edge for a lint-failed job).
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from lcp import pipeline as pl
from lcp.adapters.publisher import signoff
from lcp.adapters.storage.audit_log import AuditLog, EVENT_BATCH_SUMMARY, EVENT_REDLINE_OVERRIDE
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    TITLE,
    build_pipeline,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-24T00:00:00Z"
SOURCE_URL = "https://example.com/article"
REDLINE = "該網站內容涉及未成年色情與兒童不雅影像。\n此為違法內容請勿散佈。\n"
# A title below the 25-char lint minimum → forces NEEDS_REVISION.
SHORT_TITLE = "太短"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


@pytest.fixture()
def config():
    return Config(publisher=PublisherConfig())


def test_blocked_redline_supersede_recovery(store, audit, config):
    """REAL REDLINE source → BLOCKED (real risk gate) → supersede with redline_override → SUPERSEDED.

    Every prior supersede test seeds BLOCKED via persist_gate_state shortcut;
    this drives the real gate chain so RiskCategory-code drift is not masked."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config, source=REDLINE)
    p.stage1(spec_for(store, "blk"), ts=TS)
    res = p.process("blk", ts=TS, title=TITLE, ai_copy=True)

    assert res.final_state is JobState.BLOCKED, (
        f"expected BLOCKED from real risk gate, got {res.final_state}: {res.notes}"
    )
    assert res.stopped_at == "risk", f"stopped at {res.stopped_at!r}, expected 'risk'"

    # Ordinary supersede without redline_override must be refused.
    with pytest.raises(Exception, match="redline|override"):
        signoff.supersede("blk", store=store, audit=audit, ts=TS, redline_override=False)

    # Supersede with explicit second confirmation.
    final_state = signoff.supersede("blk", store=store, audit=audit, ts=TS, redline_override=True)
    assert final_state is JobState.SUPERSEDED
    assert store.get_job("blk").state is JobState.SUPERSEDED

    # Audit must carry REDLINE_OVERRIDE (distinct from plain SUPERSEDED).
    lines = audit._read_lines()
    override_events = [ln for ln in lines if ln.get("event") == EVENT_REDLINE_OVERRIDE]
    assert override_events, "no REDLINE_OVERRIDE audit event after redline supersede"


def test_reject_from_review_pending(store, audit, config):
    """Full flow to REVIEW_PENDING, then reject() → REJECTED with correct audit event."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, "rej"), ts=TS)
    res = p.process("rej", ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.PROCESSED, f"expected PROCESSED, got {res.final_state}: {res.notes}"

    draft = pl.load_draft(store, "rej")
    p.build_packet("rej", draft, ts=TS, source_urls=[SOURCE_URL])
    assert store.get_job("rej").state is JobState.REVIEW_PENDING

    rec = signoff.reject(
        "rej",
        "alice",
        "content quality insufficient",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert rec.new_state is JobState.REJECTED
    assert store.get_job("rej").state is JobState.REJECTED

    lines = audit._read_lines()
    reject_events = [ln for ln in lines if ln.get("event") == signoff.EVENT_SIGNOFF_REJECT]
    assert reject_events, "no SIGNOFF_REJECT audit event after reject()"


def test_audit_chain_valid_after_full_flow(store, audit, config):
    """audit.verify_chain() returns True after a complete ingest→process→approve flow."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, "chain"), ts=TS)
    res = p.process("chain", ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.PROCESSED, f"expected PROCESSED, got {res.final_state}"

    draft = pl.load_draft(store, "chain")
    p.build_packet("chain", draft, ts=TS, source_urls=[SOURCE_URL])
    signoff.approve("chain", "alice", config=config, store=store, audit=audit, ts=TS, draft=draft)

    assert audit.verify_chain(), "audit chain integrity check failed after a clean full flow"


def test_process_batch_real_chain(store, audit, config):
    """process_batch drives all CRAWLED jobs through the real gate chain independently."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)

    for job_id in ("b1", "b2"):
        p.stage1(spec_for(store, job_id), ts=TS)

    results = pl.process_batch(p, JobState.CRAWLED, ts=TS, title=TITLE, ai_copy=True)

    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    for r in results:
        assert r.final_state is JobState.PROCESSED, (
            f"job {r.final_state.value} did not reach PROCESSED: {r.notes}"
        )

    lines = audit._read_lines()
    batch_events = [ln for ln in lines if ln.get("event") == EVENT_BATCH_SUMMARY]
    assert batch_events, "no BATCH_SUMMARY audit event after process_batch"


def test_needs_revision_reprocess_recovery(store, audit, config):
    """Short title (< 25 chars) → NEEDS_REVISION via lint gate; reprocess with valid title → PROCESSED."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, "nr"), ts=TS)

    # First pass: title below lint minimum parks at NEEDS_REVISION.
    res1 = p.process("nr", ts=TS, title=SHORT_TITLE, ai_copy=True)
    assert res1.final_state is JobState.NEEDS_REVISION, (
        f"expected NEEDS_REVISION from short title, got {res1.final_state}: {res1.notes}"
    )

    # Second pass (in-place recovery): valid title clears the lint failure.
    res2 = p.process("nr", ts=TS, title=TITLE, ai_copy=True)
    assert res2.final_state is JobState.PROCESSED, (
        f"expected PROCESSED after reprocess with valid title, got {res2.final_state}: {res2.notes}"
    )
    assert store.get_job("nr").state is JobState.PROCESSED


def test_process_batch_exception_strands_marker_for_reconcile(store, audit, config):
    """process_batch catches per-job exceptions (BLE001 boundary) and leaves the
    .processing marker in place — the crashed job surfaces via reconcile() as
    interrupted rather than being silently lost (process-batch-strands-processing-marker).

    Simulated: two CRAWLED jobs; process() is patched to raise on job_1 AFTER the
    caller has pre-planted its .processing marker (replicating a mid-Stage-2 crash).
    job_2 processes normally — batch isolation is verified."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)

    p.stage1(spec_for(store, "crash_job"), ts=TS)
    p.stage1(spec_for(store, "ok_job"), ts=TS)

    # Pre-plant a stale .processing marker for crash_job (dead-pid: crash leftover).
    from lcp.adapters.storage.job_store import PROCESSING_MARKER

    marker_path = store.job_dir("crash_job") / PROCESSING_MARKER
    marker_path.write_text("2000000000", encoding="utf-8")  # dead pid

    # Patch process() to raise for crash_job only; ok_job goes through normally.
    real_process = p.process

    def _raise_for_crash(job_id, **kw):
        if job_id == "crash_job":
            raise RuntimeError("simulated mid-Stage-2 crash")
        return real_process(job_id, **kw)

    with patch.object(p, "process", side_effect=_raise_for_crash):
        results = pl.process_batch(p, JobState.CRAWLED, ts=TS, title=TITLE, ai_copy=True)

    # Only ok_job produced a result — process_batch continued past the failure.
    assert len(results) == 1
    assert results[0].job_id == "ok_job"
    assert results[0].final_state is JobState.PROCESSED

    # crash_job still has its .processing marker (not cleared by the exception path).
    assert store.is_processing("crash_job"), "stale marker must remain for reconcile() to detect"

    # reconcile() surfaces crash_job as interrupted (the dead-pid marker is a crash leftover).
    interrupted = p.reconcile()
    assert any(i.job_id == "crash_job" for i in interrupted), (
        f"crash_job not in reconcile output: {[i.job_id for i in interrupted]}"
    )
