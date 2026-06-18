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

The pure extraction policy lives in core/rules/extraction.py (strict-checked,
testable with a fabricated HtmlResponse, no network); ``extract_content`` here is
a thin adapter wrapper that injects this module's net_guard second-order SSRF
check. The spider/parse path writes source.html/source.txt + sha256 and per-asset
statuses into the bundle.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ...core.errors import InputValidationError
from ...core.models import (
    AssetKind,
    AssetRef,
    AssetState,
    SourceType,
)
from ...core.rules.extraction import classify_media_url
from ...core.rules.extraction import extract_content as _core_extract
from ..storage.manifest import write_manifest
from . import net_guard
from .base import RawJobBundle, SourceSpec
from .bundle import build_manifest, derive_status, sha256_bytes, sha256_text

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Second-order SSRF guard (I/O — stays in the adapter; injected into the pure
# core extractor, core/rules/extraction.py, which does no I/O of its own)
# --------------------------------------------------------------------------

def _media_url_is_safe(url: str) -> bool:
    """SECOND-ORDER SSRF guard: validate a scraped media URL through net_guard
    BEFORE it is handed to ImagesPipeline/FilesPipeline for download.

    The page itself was validated at the top level, but the img/video/<a> URLs it
    embeds are untrusted attacker-controlled targets (e.g. 169.254.169.254 cloud
    metadata, 127.0.0.1). Each must pass the same scheme + DNS-resolved is_global
    check, or it is dropped (recorded as a FAILED asset). LCP_ALLOW_LOOPBACK_FOR_
    TESTS keeps the loopback test fixture working, consistent with main()."""
    if os.environ.get("LCP_ALLOW_LOOPBACK_FOR_TESTS"):
        return True
    try:
        net_guard.validate_url(url)
        return True
    except InputValidationError:
        # Expected: an SSRF-unsafe / malformed target -> drop the URL quietly.
        return False
    except Exception as e:  # noqa: BLE001 - stay fail-closed but surface the bug
        # An UNEXPECTED guard error must not be silently swallowed; log it (no
        # attacker content) and still fail closed (drop the URL, never download).
        logger.warning(
            "unexpected media-URL guard error (%s); dropping URL", type(e).__name__
        )
        return False


def extract_content(response: Any) -> dict[str, Any]:
    """Adapter wrapper over the pure core extractor: run it with this adapter's
    net_guard second-order SSRF check injected. The DNS check is I/O, so it stays
    here (``_media_url_is_safe``), never in core. parse()/write_bundle and the
    crawl tests keep calling this single-arg form unchanged."""
    return _core_extract(response, is_media_url_safe=_media_url_is_safe)


# --------------------------------------------------------------------------
# Scrapy settings
# --------------------------------------------------------------------------

def build_settings(*, job_dir: Path, allow_domains: list[str], timeout: int) -> dict[str, Any]:
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

def _make_spider_cls() -> type[Any]:
    import scrapy

    class ArticleSpider(scrapy.Spider):
        name = "lcp_article"

        def __init__(self, start_url: str, allow_domains: list[str], **kw: Any) -> None:
            super().__init__(**kw)
            self.start_urls = [start_url]
            self.allowed_domains = allow_domains
            self.extracted: dict[str, Any] | None = None
            self.media_results: dict[str, Any] | None = None

        def parse(self, response: Any) -> Any:
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

def _run_spider(spec: SourceSpec, allow_domains: list[str], timeout: int) -> dict[str, Any]:
    """Run the Scrapy spider in-process (we are already in the subprocess).
    Returns the extracted dict, or {} on total failure."""
    from scrapy.crawler import CrawlerProcess

    spider_cls = _make_spider_cls()
    settings = build_settings(
        job_dir=spec.job_dir, allow_domains=allow_domains, timeout=timeout
    )
    holder: dict[str, Any] = {}

    process = CrawlerProcess(settings=settings, install_root_handler=False)

    def _item_scraped(item: Any, response: Any, spider: Any) -> None:
        # Capture the pipelines' download results ({url,path,checksum}).
        holder["images"] = item.get("images", [])
        holder["files"] = item.get("files", [])

    def _spider_closed(spider: Any) -> None:
        if getattr(spider, "extracted", None):
            holder["extracted"] = spider.extracted

    crawler = process.create_crawler(spider_cls)
    from scrapy import signals

    crawler.signals.connect(_item_scraped, signal=signals.item_scraped)
    crawler.signals.connect(_spider_closed, signal=signals.spider_closed)
    process.crawl(crawler, start_url=spec.url, allow_domains=allow_domains)
    process.start()  # blocks until done
    extracted: dict[str, Any] = holder.get("extracted", {})
    if extracted:
        extracted["downloaded_images"] = holder.get("images", [])
        extracted["downloaded_files"] = holder.get("files", [])
    return extracted


def write_bundle_from_extraction(
    spec: SourceSpec,
    extracted: dict[str, Any],
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

    # Record media URLs dropped by the SSRF guard as FAILED assets (never
    # downloaded). They count as partial-asset failures -> CRAWLED_WARN upstream.
    for url in extracted.get("rejected_media_urls", []):
        if len(assets) >= spec.max_assets:
            break
        kind = classify_media_url(url) or AssetKind.IMAGE
        assets.append(
            AssetRef(
                kind=kind,
                path="",
                source_url=url,
                state=AssetState.FAILED,
                note="rejected by SSRF guard (internal/metadata target)",
            )
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
    downloaded_images: list[dict[str, Any]],
    downloaded_files: list[dict[str, Any]],
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

    def _add(url: str, kind: AssetKind, store: Path, by_url: dict[Any, Any]) -> None:
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
    except Exception as e:  # noqa: BLE001 - child boundary: any unexpected crash
        # A non-LcpError crash (ReactorNotRestartable, MemoryError, an unexpected
        # ValueError, ...) must become a clean non-zero exit + JSON error line so the
        # parent maps it to a retriable failure, NOT an escaping traceback the parent
        # silently ignores once a (stale) manifest is present (U6).
        from ...core.errors import EXIT_INTERNAL

        print(json.dumps({"error": type(e).__name__, "message": str(e)}), file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
