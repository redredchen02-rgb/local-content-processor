"""Scrapy spider + settings + subprocess entrypoint (plan Unit 4, R6-R11, R40).

Scrapy is the FIRST Crawler implementation (the contract lives in base.py). It
runs INSIDE the per-job subprocess spawned by crawl_runner (subprocess-per-job
dodges ReactorNotRestartable + isolates crashes). Settings enforce the plan's
non-negotiables:
- ROBOTSTXT_OBEY=True (never bypass robots; on disallow -> record + skip)
- AutoThrottle on (polite rate limiting)
- allowed_domains -> OffsiteMiddleware drops off-allowlist requests
- RETRY_* + DOWNLOAD_TIMEOUT
- ImagesPipeline/FilesPipeline with IMAGES_STORE/FILES_STORE under the job dir

extract_content() is a pure function over a Scrapy Response so parse() can be
unit-tested with a fabricated HtmlResponse (no network). The spider/parse path
writes source.html/source.txt + sha256 and per-asset statuses into the bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ...core.models import (
    AssetKind,
    AssetRef,
    AssetState,
    SourceType,
)
from ..storage.manifest import write_manifest
from . import net_guard
from .base import RawJobBundle, SourceSpec
from .bundle import build_manifest, derive_status, sha256_bytes, sha256_text

# Image/video extensions used when classifying scraped media URLs.
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v", ".mkv")


# --------------------------------------------------------------------------
# Pure extraction (testable with a fabricated Scrapy Response)
# --------------------------------------------------------------------------

def _classify_media_url(url: str) -> AssetKind | None:
    low = url.lower().split("?", 1)[0]
    if low.endswith(_IMAGE_EXT):
        return AssetKind.IMAGE
    if low.endswith(_VIDEO_EXT):
        return AssetKind.VIDEO
    return None


def extract_content(response) -> dict[str, Any]:
    """Pure-ish extraction from a Scrapy Response. Returns title, body text,
    image_urls, video_urls, source_html, and resolved metadata. No network.

    De-dupes media URLs (plan edge: duplicate URLs skipped)."""
    title = (response.css("title::text").get() or "").strip()
    if not title:
        title = (response.css("h1::text").get() or "").strip()

    # Body text: prefer <article>/<main>, else all <p>.
    paras = response.css("article p::text, main p::text").getall()
    if not paras:
        paras = response.css("p::text").getall()
    body = "\n".join(t.strip() for t in paras if t.strip()).strip()

    image_urls: list[str] = []
    for src in response.css("img::attr(src)").getall():
        full = response.urljoin(src)
        if full not in image_urls:
            image_urls.append(full)

    video_urls: list[str] = []
    for src in response.css("video::attr(src), video source::attr(src)").getall():
        full = response.urljoin(src)
        if full not in video_urls:
            video_urls.append(full)
    # Also classify links pointing at media files.
    for href in response.css("a::attr(href)").getall():
        full = response.urljoin(href)
        kind = _classify_media_url(full)
        if kind is AssetKind.IMAGE and full not in image_urls:
            image_urls.append(full)
        elif kind is AssetKind.VIDEO and full not in video_urls:
            video_urls.append(full)

    return {
        "title": title,
        "body": body,
        "image_urls": image_urls,
        "video_urls": video_urls,
        "source_html": response.text,
        "metadata": {
            "url": response.url,
            "status": getattr(response, "status", None),
        },
    }


# --------------------------------------------------------------------------
# Scrapy settings
# --------------------------------------------------------------------------

def build_settings(*, job_dir: Path, allow_domains: list[str], timeout: int) -> dict:
    """Scrapy settings dict enforcing the plan's crawl policy."""
    raw = job_dir / "raw"
    return {
        "ROBOTSTXT_OBEY": True,             # never bypass robots (R8)
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "DOWNLOAD_TIMEOUT": timeout,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 2,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "REDIRECT_ENABLED": False,          # do not blindly follow redirects (R40)
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "ERROR",
        "USER_AGENT": "local-content-processor/0.1 (+respects robots.txt)",
        "ITEM_PIPELINES": {
            "scrapy.pipelines.images.ImagesPipeline": 1,
            "scrapy.pipelines.files.FilesPipeline": 2,
        },
        "IMAGES_STORE": str(raw / "images"),
        "FILES_STORE": str(raw / "videos"),
        "DEPTH_LIMIT": 1,
    }


# --------------------------------------------------------------------------
# Spider (imported lazily so the module imports without scrapy where unused)
# --------------------------------------------------------------------------

def _make_spider_cls():
    import scrapy

    class ArticleSpider(scrapy.Spider):
        name = "lcp_article"

        def __init__(self, start_url: str, allow_domains: list[str], **kw):
            super().__init__(**kw)
            self.start_urls = [start_url]
            self.allowed_domains = allow_domains
            self.extracted: dict | None = None
            self.media_results: dict | None = None

        def parse(self, response):
            extracted = extract_content(response)
            self.extracted = extracted
            # Yield ONE item carrying media URLs so ImagesPipeline (image_urls
            # -> images) and FilesPipeline (file_urls -> files) download them
            # into IMAGES_STORE/FILES_STORE. The pipelines fill `images`/`files`
            # with {url, path, checksum} which we capture in the item_scraped
            # signal for precise per-asset mapping.
            yield {
                "image_urls": extracted["image_urls"],
                "file_urls": extracted["video_urls"],
            }

    return ArticleSpider


# --------------------------------------------------------------------------
# Subprocess entrypoint
# --------------------------------------------------------------------------

def _run_spider(spec: SourceSpec, allow_domains: list[str], timeout: int) -> dict:
    """Run the Scrapy spider in-process (we are already in the subprocess).
    Returns the extracted dict, or {} on total failure."""
    from scrapy.crawler import CrawlerProcess

    spider_cls = _make_spider_cls()
    settings = build_settings(
        job_dir=spec.job_dir, allow_domains=allow_domains, timeout=timeout
    )
    holder: dict[str, dict] = {}

    process = CrawlerProcess(settings=settings, install_root_handler=False)

    def _item_scraped(item, response, spider):
        # Capture the pipelines' download results ({url,path,checksum}).
        holder["images"] = item.get("images", [])
        holder["files"] = item.get("files", [])

    def _spider_closed(spider):
        if getattr(spider, "extracted", None):
            holder["extracted"] = spider.extracted

    crawler = process.create_crawler(spider_cls)
    from scrapy import signals

    crawler.signals.connect(_item_scraped, signal=signals.item_scraped)
    crawler.signals.connect(_spider_closed, signal=signals.spider_closed)
    process.crawl(crawler, start_url=spec.url, allow_domains=allow_domains)
    process.start()  # blocks until done
    extracted = holder.get("extracted", {})
    if extracted:
        extracted["downloaded_images"] = holder.get("images", [])
        extracted["downloaded_files"] = holder.get("files", [])
    return extracted


def write_bundle_from_extraction(
    spec: SourceSpec,
    extracted: dict,
    *,
    source_domain: str | None,
    fetched_at: str | None,
) -> RawJobBundle:
    """Persist source.{html,txt} + per-asset manifest from an extraction dict.

    Asset paths reference whatever ImagesPipeline/FilesPipeline wrote under
    raw/images and raw/videos. We record per-asset OK/FAILED by checking which
    declared media URLs produced a file. create_only guards against clobber."""
    raw = spec.job_dir / "raw"
    images = raw / "images"
    videos = raw / "videos"
    for d in (raw, images, videos):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

    title = extracted.get("title") or ""
    body = extracted.get("body") or ""
    source_html = extracted.get("source_html")
    if source_html is not None:
        _write_0600(raw / "source.html", source_html.encode("utf-8"))
    _write_0600(raw / "source.txt", body.encode("utf-8"))
    if extracted.get("metadata") is not None:
        _write_0600(
            raw / "metadata.json",
            json.dumps(extracted["metadata"], ensure_ascii=False, indent=2).encode("utf-8"),
        )

    assets = _assets_from_pipeline_output(
        images_store=images,
        videos_store=videos,
        image_urls=extracted.get("image_urls", []),
        video_urls=extracted.get("video_urls", []),
        downloaded_images=extracted.get("downloaded_images", []),
        downloaded_files=extracted.get("downloaded_files", []),
        job_dir=spec.job_dir,
        max_assets=spec.max_assets,
    )

    status = derive_status(title=title, body=body, assets=assets)
    manifest = build_manifest(
        job_id=spec.job_id,
        source_type=spec.source_type if spec.source_type else SourceType.URL,
        source_domain=source_domain,
        fetched_at=fetched_at,
        assets=assets,
        source_html=source_html,
        source_text=body,
        crawl_status=status,
    )
    write_manifest(spec.job_dir, manifest, create_only=True)
    return RawJobBundle(
        job_id=spec.job_id, raw_dir=raw, manifest=manifest, job_status=status
    )


def _assets_from_pipeline_output(
    *,
    images_store: Path,
    videos_store: Path,
    image_urls: list[str],
    video_urls: list[str],
    downloaded_images: list[dict],
    downloaded_files: list[dict],
    job_dir: Path,
    max_assets: int,
) -> list[AssetRef]:
    """Map declared media URLs to per-asset OK/FAILED outcomes (plan G2).

    Scrapy's ImagesPipeline/FilesPipeline return per-download dicts
    {url, path, checksum}. We map each declared URL to its result by URL; a
    declared URL with NO successful download is recorded FAILED (partial-asset
    failure -> CRAWLED_WARN upstream)."""
    assets: list[AssetRef] = []
    img_by_url = {d.get("url"): d for d in downloaded_images if isinstance(d, dict)}
    vid_by_url = {d.get("url"): d for d in downloaded_files if isinstance(d, dict)}

    def _add(url: str, kind: AssetKind, store: Path, by_url: dict) -> None:
        if len(assets) >= max_assets:
            return
        result = by_url.get(url)
        if result and result.get("path"):
            disk_path = store / result["path"]  # path is relative to STORE
            try:
                data = disk_path.read_bytes()
                os.chmod(disk_path, 0o600)
                sha = sha256_bytes(data)
                rel = disk_path.relative_to(job_dir).as_posix()
                assets.append(
                    AssetRef(kind=kind, path=rel, source_url=url,
                             sha256=sha, state=AssetState.OK)
                )
                return
            except OSError:
                pass
        assets.append(
            AssetRef(
                kind=kind,
                path="",
                source_url=url,
                state=AssetState.FAILED,
                note="download produced no file (robots/anti-bot/network)",
            )
        )

    for url in image_urls:
        _add(url, AssetKind.IMAGE, images_store, img_by_url)
    for url in video_urls:
        _add(url, AssetKind.VIDEO, videos_store, vid_by_url)
    return assets


def _write_0600(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Subprocess entrypoint: `python -m lcp.adapters.crawler.scrapy_impl ...`.

    Validates the URL via net_guard (SSRF) BEFORE Scrapy touches the network,
    runs the spider, writes the bundle, and prints a JSON result line to stdout
    for the parent crawl_runner. Exit code follows the LcpError contract."""
    parser = argparse.ArgumentParser(prog="lcp-crawl")
    parser.add_argument("--url", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--allow-domain", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--source-domain", default=None)
    parser.add_argument("--fetched-at", default=None)
    args = parser.parse_args(argv)

    from ...core.errors import LcpError
    from ...runtime_hardening import set_restrictive_umask

    set_restrictive_umask()  # ensure 0600 even if spawned fresh

    spec = SourceSpec(
        job_id=args.job_id,
        source_type=SourceType.URL,
        job_dir=Path(args.job_dir),
        url=args.url,
    )
    try:
        # SSRF pre-flight (real DNS here). LCP_ALLOW_LOOPBACK_FOR_TESTS is a
        # TEST-ONLY escape so the integration test can fetch a 127.0.0.1
        # http.server fixture; production never sets it and the parent
        # crawl_runner's preflight still enforces the full guard.
        if not os.environ.get("LCP_ALLOW_LOOPBACK_FOR_TESTS"):
            net_guard.validate_url(args.url)
        extracted = _run_spider(spec, args.allow_domain, args.timeout)
        bundle = write_bundle_from_extraction(
            spec,
            extracted or {"title": "", "body": "", "metadata": {"url": args.url}},
            source_domain=args.source_domain,
            fetched_at=args.fetched_at,
        )
        print(json.dumps({"status": bundle.job_status, "job_id": bundle.job_id}))
        return 0
    except LcpError as e:
        print(json.dumps({"error": type(e).__name__, "message": str(e)}), file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
