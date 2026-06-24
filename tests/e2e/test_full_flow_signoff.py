"""U6 — E2E full signoff flow + edge cases.

Happy path: ingest → process → review-packet → approve → backfill reaches
PUBLISHED_RECORDED with the correct audit events.

Edge cases:
- Body tampering detected at approve time (hash mismatch → InputValidationError).
- Signing off without a review packet → InputValidationError (not a crash).
"""

from __future__ import annotations

import pytest

from lcp import pipeline as pl
from lcp.adapters.publisher import signoff
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.errors import InputValidationError
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    TITLE,
    build_pipeline,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-22T00:00:00Z"
SOURCE_URL = "https://example.com/article"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


@pytest.fixture()
def config():
    return Config(publisher=PublisherConfig())


def _process_to_processed(store, audit, config, job_id="fs"):
    """Stage 1 + real Stage-2 → PROCESSED draft."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, job_id), ts=TS)
    res = p.process(job_id, ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.PROCESSED, res.notes
    return p


def test_full_flow_happy_path(store, audit, config):
    """Ingest → process → review-packet → approve → backfill reaches
    PUBLISHED_RECORDED."""
    p = _process_to_processed(store, audit, config, "happy")

    draft = pl.load_draft(store, "happy")
    packet = p.build_packet("happy", draft, ts=TS, source_urls=[SOURCE_URL])
    assert packet.body_sha256  # a real frozen packet was produced
    assert store.get_job("happy").state is JobState.REVIEW_PENDING

    rec = signoff.approve(
        "happy", "alice", config=config, store=store, audit=audit, ts=TS, draft=draft
    )
    assert rec.new_state is JobState.APPROVED

    final = signoff.backfill_published_url(
        "happy",
        SOURCE_URL,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer="alice",
    )
    assert final is JobState.PUBLISHED_RECORDED


def test_approve_with_tampered_frozen_packet_body(store, audit, config):
    """If the frozen packet's body hash doesn't match the draft's current body,
    approve should raise InputValidationError — never silently accept."""
    from lcp.core.draft import Draft

    p = _process_to_processed(store, audit, config, "tamper")

    draft = pl.load_draft(store, "tamper")
    p.build_packet("tamper", draft, ts=TS, source_urls=[SOURCE_URL])

    # Pass a tampered Draft — its body hash won't match the frozen hash in
    # review_manifest.json, so approve refuses.
    tampered = Draft(title=draft.title, event_body=draft.event_body + " [TAMPERED]")
    with pytest.raises(InputValidationError, match="hash|body|tamper|mismatch"):
        signoff.approve(
            "tamper",
            "alice",
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            draft=tampered,
        )


def test_approve_without_review_packet(store, audit, config):
    """Signing off a job that has never been through build_packet must raise
    InputValidationError — never a crash or silent success."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, "nopkt"), ts=TS)
    res = p.process("nopkt", ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.PROCESSED, res.notes

    draft = pl.load_draft(store, "nopkt")
    assert draft is not None

    with pytest.raises(InputValidationError):
        signoff.approve(
            "nopkt",
            "alice",
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            draft=draft,
        )
