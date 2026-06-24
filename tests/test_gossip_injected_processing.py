"""Unit 5: a gossip-injected job flows through the UNCHANGED Stage-2 gate chain
to a frozen review packet (REVIEW_PENDING), ready for the existing manual
approve/attest publish path.

Drives the REAL gate chain (risk → media → dedup → assemble → copywriter → lint
→ ground) via the no-network FakeCrawler + deterministic DualModeChatClient — no
persist_gate_state shortcut. Starts from the ingest seam (source.json), so this
also proves the persisted-URL → crawl-by-id resolution end-to-end.

The FakeCrawler writes no images, so this is a text-only bundle (image_sections
conditional, D9): cover-from-real-image + watermark-on-real-image need a real
crawl and remain integration-deferred (as the plan notes)."""

from __future__ import annotations

import lcp.pipeline as pl
from lcp.adapters.crawler.base import SourceSpec
from lcp.adapters.storage import gossip_ingest as gi
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.models import SourceType
from lcp.core.state import JobState
from tests.support.pipeline_fakes import TITLE, build_pipeline, seed_clean_index

TS = "2026-06-22T00:00:00Z"


def _setup(tmp_path):
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    seed_clean_index(store)  # empty index -> UNIQUE at the dedup honesty gate
    return store, audit


def _spec_from_ingest(store, job_id) -> SourceSpec:
    url = gi.read_source_url(store.job_dir(job_id))
    assert url, "ingest must persist the source URL"
    return SourceSpec(
        job_id=job_id,
        source_type=SourceType.URL,
        job_dir=store.job_dir(job_id),
        url=url,
    )


def test_injected_job_reaches_review_pending(tmp_path):
    store, audit = _setup(tmp_path)
    report = gi.ingest_items(
        [{"platform": "weibo", "title": "市集", "url": "https://s.weibo.com/weibo?q=市集"}],
        store,
        ts=TS,
    )
    jid = report.created[0]
    assert store.get_job(jid).state is JobState.NEW

    config = Config(publisher=PublisherConfig())
    p = build_pipeline(store, audit, config=config)
    # An explicit working title isolates "does the gate chain run" from title
    # GENERATION (gossip hot-search phrases are too short for lint's 25-35 char
    # title — the copywriter must expand them; that is a U6 Eatmelon-prompt task).
    res = p.run_until(
        _spec_from_ingest(store, jid),
        target=pl.TARGET_REVIEW,
        ts=TS,
        title=TITLE,
        ai_copy=True,
    )

    assert res.final_state is JobState.REVIEW_PENDING, res.notes
    assert res.packet is not None  # a frozen review packet was built
    assert res.packet.body_sha256  # with a real body


def test_injected_job_runs_real_gate_chain_to_processed(tmp_path):
    # target=draft stops at PROCESSED (no packet) — proves the gate chain itself
    # runs on an injected job, not just the packet step.
    store, audit = _setup(tmp_path)
    jid = gi.ingest_items(
        [{"platform": "douyin", "title": "瓜", "url": "https://www.douyin.com/search/瓜"}],
        store,
        ts=TS,
    ).created[0]
    p = build_pipeline(store, audit, config=Config(publisher=PublisherConfig()))
    res = p.run_until(
        _spec_from_ingest(store, jid),
        target=pl.TARGET_DRAFT,
        ts=TS,
        title=TITLE,
        ai_copy=True,
    )
    assert res.final_state is JobState.PROCESSED, res.notes
