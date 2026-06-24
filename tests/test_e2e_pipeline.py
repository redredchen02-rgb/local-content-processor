"""Durable end-to-end test: ONE job through the REAL Stage-2 gate chain.

This is the standing regression guard for the masking-bug class (plan Unit 2,
docs/solutions/unit-tests-mask-integration-bugs.md). It drives ingest ->
process -> review-packet -> approve -> backfill to PUBLISHED_RECORDED, with
`process` running the real risk -> media -> dedup -> assemble -> copywriter ->
lint -> ground chain to a *substantive* PROCESSED draft — NEVER via
persist_gate_state (that shortcut is what masked B0). If any gate is silently
skipped or a required section loses its producer again, this test fails.
"""

from __future__ import annotations

import json

import pytest

from lcp import pipeline as pl
from lcp.adapters.publisher import signoff
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    SOURCE,
    TITLE,
    build_pipeline,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-18T00:00:00Z"
SOURCE_URL = "https://example.com/article"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


@pytest.fixture()
def config():
    # A whitelisted reviewer so approve/backfill can complete; categories stay
    # empty (Config default) so lint skips the category check.
    return Config(publisher=PublisherConfig())


def _process_to_processed(store, audit, config, job_id="e2e"):
    """Stage 1 + real Stage-2 (ai_copy=True) -> a PROCESSED draft."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, job_id), ts=TS)
    res = p.process(job_id, ts=TS, title=TITLE, ai_copy=True)
    return p, res


def test_full_chain_reaches_processed_via_real_gates(store, audit, config):
    """The first time any test reaches PROCESSED through the real gate chain."""
    p, res = _process_to_processed(store, audit, config)

    assert res.final_state is JobState.PROCESSED, res.notes
    assert store.get_job("e2e").state is JobState.PROCESSED

    draft = pl.load_draft(store, "e2e")
    assert draft is not None
    # Proof the REAL assemble+copywriter ran (not persist_gate_state): the draft
    # is executed and the formerly-orphaned required sections are populated.
    assert draft.executed is True
    assert draft.quick_facts and draft.summary and draft.tags
    assert 3 <= len(draft.tags) <= 5
    assert draft.event_body and draft.faq


def test_packet_freeze_then_approve_then_backfill(store, audit, config):
    p, res = _process_to_processed(store, audit, config)
    assert res.final_state is JobState.PROCESSED

    draft = pl.load_draft(store, "e2e")
    packet = p.build_packet("e2e", draft, ts=TS, source_urls=[SOURCE_URL])
    assert store.get_job("e2e").state is JobState.REVIEW_PENDING
    assert packet.body_sha256  # a real frozen body hash

    rec = signoff.approve(
        "e2e", "alice", config=config, store=store, audit=audit, ts=TS, draft=draft
    )
    assert rec.new_state is JobState.APPROVED

    final = signoff.backfill_published_url(
        "e2e",
        SOURCE_URL,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer="alice",
    )
    assert final is JobState.PUBLISHED_RECORDED


def test_frozen_packet_and_manifest_are_wellformed_json(store, audit, config):
    """Atomic-write proof (R2): the freeze artifacts exist and parse cleanly."""
    p, _ = _process_to_processed(store, audit, config)
    draft = pl.load_draft(store, "e2e")
    p.build_packet("e2e", draft, ts=TS, source_urls=[SOURCE_URL])

    job_dir = store.job_dir("e2e")
    manifests = list(job_dir.rglob("*.json"))
    assert manifests, "no JSON artifacts written by the freeze"
    for m in manifests:
        json.loads(m.read_text(encoding="utf-8"))  # raises if torn/half-written


def test_run_until_review_defaults_ai_copy_on(store, audit, config):
    """D2: `run --until review` defaults --ai-copy ON, so the one-shot operator
    path reaches a frozen packet (REVIEW_PENDING) without the dead-end."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    res = p.run_until(
        spec_for(store, "r1"),
        target="review",
        ts=TS,
        title=TITLE,
        source_urls=[SOURCE_URL],
    )
    assert res.final_state is JobState.REVIEW_PENDING, res.notes
    assert res.packet is not None and res.packet.body_sha256


def test_run_until_review_no_ai_copy_dead_ends(store, audit, config):
    """The opposite: --no-ai-copy can't produce a complete draft (the copywriter
    sections stay empty) -> parks at NEEDS_REVISION, never PROCESSED."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    res = p.run_until(
        spec_for(store, "r2"),
        target="review",
        ts=TS,
        title=TITLE,
        ai_copy=False,
    )
    assert res.final_state is not JobState.REVIEW_PENDING


def test_job_matching_site_index_is_detected_duplicate(store, audit, config):
    """The site index is actually consulted: a job whose body already exists in
    the index is parked DUPLICATE by the real dedup gate (proves the gate runs,
    not that it is bypassed)."""
    # Seed the index WITH this job's body+title -> the dedup gate must match it.
    entry = json.dumps({"job_id": "prior", "title": TITLE, "body": SOURCE}, ensure_ascii=False)
    (store.base_dir).mkdir(parents=True, exist_ok=True)
    (store.base_dir / "site_index.jsonl").write_text(entry + "\n", encoding="utf-8")

    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, "dup"), ts=TS)
    res = p.process("dup", ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.DUPLICATE, res.notes
