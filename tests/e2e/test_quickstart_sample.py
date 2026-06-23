"""U4: the documented `lcp ingest --dir ./samples/demo-001` quickstart actually runs.

Drives the REAL `LocalIngestCrawler` over the committed text-only sample folder
through the real Stage-2 gate chain to EXACTLY `REVIEW_PENDING` — never via
`persist_gate_state`, never asserting a state disjunction (the masking-bug trap,
docs/solutions/real-happy-path-unreachable-masked-by-green-tests.md).

The sample is text-only on purpose: the real-decodable-image media path is proven
separately in `test_real_image_media_gate.py`. Coupling an image into this sample
would make `image_sections` lint-required (has_images=True) and force a grounded
CAPTION fixture — the fragility the U4 decoupling deliberately avoids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcp import pipeline as pl
from lcp.adapters.crawler.base import SourceSpec
from lcp.adapters.crawler.ingest import LocalIngestCrawler
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.models import SourceType
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    LOOSE_CONTENT_CONFIG,
    SOURCE,
    TITLE,
    DualModeChatClient,
    seed_clean_index,
)

TS = "2026-06-22T00:00:00Z"
SOURCE_URL = "https://example.com/demo-001"
SAMPLE_DIR = Path(__file__).resolve().parents[2] / "samples" / "demo-001"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


@pytest.fixture()
def config():
    # Use loose Unit-1 content constraints: the fake LLM generates minimal
    # content for determinism; field-length correctness is tested in
    # tests/rules/test_lint_rules.py (not the goal of this quickstart e2e).
    return Config(
        publisher=PublisherConfig(reviewers=["alice"]),
        content=LOOSE_CONTENT_CONFIG,
    )


def test_sample_matches_grounded_fixture() -> None:
    """Drift guard: the committed sample must equal the grounded fixture text, or
    the deterministic fake LLM (its claims are verbatim SOURCE substrings) would
    fail the grounding gate and this quickstart e2e would silently stop passing.
    """
    assert (SAMPLE_DIR / "title.txt").read_text(encoding="utf-8").strip() == TITLE
    assert (SAMPLE_DIR / "body.txt").read_text(encoding="utf-8").strip() == SOURCE


def test_quickstart_sample_reaches_review_pending(store, audit, config) -> None:
    """`lcp ingest --dir ./samples/demo-001` → process → review-packet, driven
    through the REAL ingest + Stage-2 chain, reaches EXACTLY REVIEW_PENDING."""
    seed_clean_index(store)
    title = (SAMPLE_DIR / "title.txt").read_text(encoding="utf-8").strip()
    pipeline = pl.Pipeline(
        config,
        store,
        audit,
        crawler=LocalIngestCrawler(),
        llm_client=DualModeChatClient(),
    )
    spec = SourceSpec(
        job_id="demo-001",
        source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir("demo-001"),
        local_dir=SAMPLE_DIR,
    )

    res = pipeline.run_until(spec, target="review", ts=TS, title=title, source_urls=[SOURCE_URL])

    assert res.final_state is JobState.REVIEW_PENDING, res.notes
    assert res.packet is not None and res.packet.body_sha256


def test_quickstart_ingest_refuses_overwrite(store, audit, config) -> None:
    """A second ingest of the same job-id is refused (create-only) — the 'use a
    fresh job id' requirement the quickstart depends on."""
    from lcp.core.errors import InputValidationError

    spec = SourceSpec(
        job_id="demo-001",
        source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir("demo-001"),
        local_dir=SAMPLE_DIR,
    )
    LocalIngestCrawler().crawl(spec)
    with pytest.raises(InputValidationError):
        LocalIngestCrawler().crawl(spec)
