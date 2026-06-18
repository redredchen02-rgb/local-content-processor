"""Unit 7: the production crawl seam (Pipeline.stage1 -> build_crawler ->
CrawlRunner) is SSRF-safe.

The existing real-subprocess crawl test (tests/test_crawl_runner.py::
test_real_subprocess_crawl_produces_bundle_0600_sha256) drives a REAL Scrapy
subprocess hermetically — but it spawns `scrapy_impl.main` directly, bypassing
the production seam, because `minimal_env` deliberately SCRUBS the
LCP_ALLOW_LOOPBACK_FOR_TESTS escape (so a positive loopback crawl through the
seam is correctly impossible). This unit covers the seam's negative side: a
loopback/internal target is REJECTED through Pipeline.stage1, and the test escape
is never honored at the seam (it is subprocess-direct-only). The SSRF preflight
rejects before any connection, so no live server is needed.
"""

from __future__ import annotations

import pytest

from lcp import pipeline as pl
from lcp.adapters.crawler.base import SourceSpec
from lcp.adapters.crawler.factory import build_crawler
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, CrawlerConfig
from lcp.core.errors import InputValidationError
from lcp.core.models import SourceType

TS = "2026-06-18T00:00:00Z"
LOOPBACK_URL = "http://127.0.0.1:9/article.html"  # rejected before any connect


def _stage1_loopback(tmp_path):
    # Allowlist 127.0.0.1 so the rejection is the SSRF is_global check (not the
    # domain allowlist) — proving the SSRF guard, not just the allowlist, fires.
    config = Config(crawler=CrawlerConfig(allow_domains=["127.0.0.1"]))
    store = JobStore(base_dir=tmp_path / "data")
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    crawler = build_crawler(config, audit, lambda: TS)
    p = pl.Pipeline(config, store, audit, crawler=crawler)
    spec = SourceSpec(
        job_id="ssrf",
        source_type=SourceType.URL,
        job_dir=store.job_dir("ssrf"),
        url=LOOPBACK_URL,
        max_assets=10,
    )
    p.stage1(spec, ts=TS)


def test_stage1_rejects_loopback_with_escape_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LCP_ALLOW_LOOPBACK_FOR_TESTS", raising=False)
    with pytest.raises(InputValidationError):
        _stage1_loopback(tmp_path)


def test_stage1_rejects_loopback_even_with_escape_set(tmp_path, monkeypatch):
    # The seam (crawl_runner preflight) does NOT honor the test escape — only the
    # directly-spawned scrapy_impl does. So even an INHERITED escape var cannot
    # turn a production crawl into an SSRF.
    monkeypatch.setenv("LCP_ALLOW_LOOPBACK_FOR_TESTS", "1")
    with pytest.raises(InputValidationError):
        _stage1_loopback(tmp_path)
