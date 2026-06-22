"""Proves the crawler seam is real (plan 架構審查 7 / 證明接縫真實).

A fake in-memory Crawler implementing base.py works wherever the Scrapy impl
would: it satisfies both the ABC and the runtime-checkable Protocol, and a
pipeline function written against the contract consumes its RawJobBundle
without knowing which implementation produced it.
"""

from __future__ import annotations

from lcp.adapters.crawler.base import (
    STATUS_CRAWLED,
    Crawler,
    CrawlerProtocol,
    RawJobBundle,
    SourceSpec,
)
from lcp.adapters.crawler.bundle import build_manifest
from lcp.adapters.crawler.ingest import LocalIngestCrawler
from lcp.core.models import AssetKind, AssetRef, AssetState, SourceType


class FakeCrawler(Crawler):
    """In-memory crawler: no network, no disk reads — pure fixture."""

    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        assets = [
            AssetRef(
                kind=AssetKind.IMAGE, path="raw/images/a.jpg", sha256="0" * 64, state=AssetState.OK
            ),
        ]
        manifest = build_manifest(
            job_id=spec.job_id,
            source_type=spec.source_type,
            source_domain="fake.example",
            fetched_at="2026-06-16T00:00:00Z",
            assets=assets,
            source_html="<html><title>t</title><body>b</body></html>",
            source_text="body text",
            crawl_status=STATUS_CRAWLED,
        )
        return RawJobBundle(
            job_id=spec.job_id,
            raw_dir=spec.job_dir / "raw",
            manifest=manifest,
            job_status=STATUS_CRAWLED,
        )


def consume_bundle(crawler: CrawlerProtocol, spec: SourceSpec) -> int:
    """A pipeline step written against the CONTRACT only — it never names a
    concrete implementation. Returns the number of OK assets."""
    bundle = crawler.crawl(spec)
    assert isinstance(bundle, RawJobBundle)
    return sum(1 for a in bundle.assets if a.state is AssetState.OK)


def test_fake_satisfies_abc_and_protocol():
    fake = FakeCrawler()
    assert isinstance(fake, Crawler)
    assert isinstance(fake, CrawlerProtocol)


def test_local_ingest_also_satisfies_contract():
    assert issubclass(LocalIngestCrawler, Crawler)
    assert isinstance(LocalIngestCrawler(), CrawlerProtocol)


def test_pipeline_uses_fake_interchangeably(tmp_path):
    spec = SourceSpec(
        job_id="j1",
        source_type=SourceType.URL,
        job_dir=tmp_path / "j1",
        url="https://fake.example/x",
    )
    ok = consume_bundle(FakeCrawler(), spec)
    assert ok == 1


def test_pipeline_uses_real_ingest_interchangeably(tmp_path):
    # The same consume_bundle() that drove FakeCrawler drives a real impl.
    src = tmp_path / "material"
    src.mkdir()
    (src / "title.txt").write_text("A real title", encoding="utf-8")
    (src / "body.txt").write_text("Real body content", encoding="utf-8")
    (src / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    spec = SourceSpec(
        job_id="j2",
        source_type=SourceType.LOCAL_DIR,
        job_dir=tmp_path / "j2",
        local_dir=src,
    )
    (tmp_path / "j2").mkdir()
    ok = consume_bundle(LocalIngestCrawler(), spec)
    assert ok == 1
