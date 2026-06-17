"""build_crawler factory: the single wiring of the URL-crawl adapter (U3)."""

from lcp.adapters.clock import now
from lcp.adapters.crawler.crawl_runner import CrawlRunnerCrawler
from lcp.adapters.crawler.factory import build_crawler
from lcp.adapters.storage.audit_log import AuditLog
from lcp.core.config import Config, CrawlerConfig


def test_build_crawler_wires_runner_from_config(tmp_path):
    cfg = Config(crawler=CrawlerConfig(allow_domains=["a.com"], timeout_seconds=17))
    audit = AuditLog(tmp_path / "audit.jsonl")
    crawler = build_crawler(cfg, audit, now)
    assert isinstance(crawler, CrawlRunnerCrawler)
    # the config timeout, the injected audit, and the boundary clock are wired in
    assert crawler._runner.timeout == 17
    assert crawler._runner.audit is audit
    assert crawler._ts is now
