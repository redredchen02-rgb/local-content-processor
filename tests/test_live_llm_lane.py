"""Unit 8: opt-in live-LLM validation lane (closes deferred PR #5 line 81).

Default-SKIPPED. Runs only when a real OpenAI-compatible endpoint + secret are
provided via env, so CI (which injects no secret) always skips it and the
deterministic suite is unaffected. When enabled, it validates the REAL round-trip
— assemble + structural copy (incl. the new tags/quick_facts/summary) + grounding
— against a small SYNTHETIC fixture (never real scraped subject PII).

Enable by exporting:
  LCP_LLM_API_KEY         (the secret; read exactly as production does)
  LCP_LIVE_LLM_BASE_URL   (OpenAI-compatible base, must include /v1)
  LCP_LIVE_LLM_MODEL      (model id)
then run:  ./.venv/bin/python -m pytest tests/test_live_llm_lane.py -q
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

from lcp import pipeline as pl
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, LlmConfig, PublisherConfig
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    SOURCE,
    TITLE,
    FakeCrawler,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-18T00:00:00Z"
_LIVE_ENV = ("LCP_LLM_API_KEY", "LCP_LIVE_LLM_BASE_URL", "LCP_LIVE_LLM_MODEL")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(k) for k in _LIVE_ENV),
    reason="live-LLM lane: set LCP_LLM_API_KEY + LCP_LIVE_LLM_BASE_URL + "
    "LCP_LIVE_LLM_MODEL to run (default-skipped, e.g. in CI)",
)


def _live_config() -> Config:
    base_url = os.environ["LCP_LIVE_LLM_BASE_URL"]
    host = urlparse(base_url).hostname or ""
    return Config(
        llm=LlmConfig(
            base_url=base_url,
            model=os.environ["LCP_LIVE_LLM_MODEL"],
            allowed_hosts=[host],
        ),
        publisher=PublisherConfig(reviewers=["alice"]),
    )


def test_live_endpoint_round_trip(tmp_path):
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    seed_clean_index(store)
    # Real LlmClient (NOT injected) built from config + LCP_LLM_API_KEY.
    p = pl.Pipeline(_live_config(), store, audit, crawler=FakeCrawler(SOURCE))
    p.stage1(spec_for(store, "live"), ts=TS)
    res = p.process("live", ts=TS, title=TITLE, ai_copy=True)

    # The model was actually called and produced a substantive draft.
    assert res.draft is not None and res.draft.executed is True
    # A real model may paraphrase below the grounding overlap threshold and
    # correctly park at NEEDS_HUMAN_REVIEW — accept PROCESSED OR a typed hold,
    # but NEVER an unexpected crash. (Strict PROCESSED is asserted only by the
    # deterministic fake-client e2e, not here.)
    assert res.final_state in (
        JobState.PROCESSED,
        JobState.NEEDS_HUMAN_REVIEW,
        JobState.NEEDS_REVISION,
    ), res.notes
