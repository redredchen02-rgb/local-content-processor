"""U6 — E2E dedup: real gate chain with pre-existing site index.

The dedup gate consults the *pre-existing* site index (jobs published/known
before the current job runs). It does NOT auto-populate the index from
processed-but-unpublished jobs.

Test 1: seed index with matching content → process → DUPLICATE (the index lookup
         works through the real gate chain with a real FakeCrawler).
Test 2: clean index → process → PROCESSED (the gate correctly passes unique
         content).
Test 3: seed index with one entry, process a *different* job → PROCESSED (no
         false duplicate).
"""

from __future__ import annotations

import json

import pytest

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

TS = "2026-06-22T00:00:00Z"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


@pytest.fixture()
def config():
    return Config(publisher=PublisherConfig())


def _write_index(store: JobStore, job_id: str, body: str) -> None:
    """Write a single entry into the site index."""
    entry = json.dumps({"job_id": job_id, "title": TITLE, "body": body}, ensure_ascii=False)
    (store.base_dir / "site_index.jsonl").write_text(entry + "\n", encoding="utf-8")


def test_dedup_matches_via_site_index(store, audit, config):
    """Seed the index with an existing job matching the current source → dedup
    gate parks at DUPLICATE."""
    _write_index(store, "prior", SOURCE)

    p = build_pipeline(store, audit, config=config, source=SOURCE)
    p.stage1(spec_for(store, "dup"), ts=TS)
    res = p.process("dup", ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.DUPLICATE, f"not DUPLICATE: {res.notes}"
    assert "dedup" in (res.stopped_at or ""), f"stopped at {res.stopped_at}, expected dedup"


def test_unique_content_with_clean_index(store, audit, config):
    """Clean (empty) site index → PROCESSED through the real gate chain."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config, source=SOURCE)
    p.stage1(spec_for(store, "unique"), ts=TS)
    res = p.process("unique", ts=TS, title=TITLE, ai_copy=True)
    assert res.final_state is JobState.PROCESSED, f"unique content not processed: {res.notes}"


def test_different_content_not_deduped(store, audit, config):
    """Seed the index with one entry, process a *different* job → not a false
    duplicate."""
    _write_index(store, "other", "一些完全不同的內容。\n")

    OTHER_SOURCE = "今日台北天氣晴朗氣溫約三十度。\n週末預計有午後雷陣雨。\n"

    p = build_pipeline(store, audit, config=config, source=OTHER_SOURCE)
    p.stage1(spec_for(store, "j1"), ts=TS)
    TITLE2 = "台北今日天氣晴朗週末預計有午後雷陣雨出門帶傘"
    res = p.process("j1", ts=TS, title=TITLE2, ai_copy=True)

    # The dedup gate should NOT match (different body text), BUT the
    # quality/length checks have a lower bar and may still park the job. The only
    # assertion is: NOT DUPLICATE (not a false positive dedup).
    assert res.final_state is not JobState.DUPLICATE, f"false duplicate: {res.notes}"
