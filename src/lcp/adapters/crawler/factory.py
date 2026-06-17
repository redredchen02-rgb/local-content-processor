"""Crawler construction factory: the one place the URL-crawl adapter is wired.

Both shells built the identical ``SourceRegistry -> CrawlRunner ->
CrawlRunnerCrawler`` chain (verbatim, 3x); this is the single copy (plan 004 U3).
``ts_provider`` is the shell's boundary clock (``adapters.clock.now``)."""

from __future__ import annotations

from collections.abc import Callable

from ...core.config import Config
from ..storage.audit_log import AuditLog
from .crawl_runner import CrawlRunner, CrawlRunnerCrawler
from .source_registry import SourceRegistry


def build_crawler(
    config: Config, audit: AuditLog, ts_provider: Callable[[], str]
) -> CrawlRunnerCrawler:
    """Wire the URL-crawl adapter: an allowlist registry + the per-job
    CrawlRunner (subprocess isolation) wrapped in the CrawlRunnerCrawler that the
    ``Pipeline.stage1`` seam consumes."""
    registry = SourceRegistry.from_config(config.crawler)
    return CrawlRunnerCrawler(
        CrawlRunner(registry, timeout=config.crawler.timeout_seconds, audit=audit),
        ts_provider=ts_provider,
    )
