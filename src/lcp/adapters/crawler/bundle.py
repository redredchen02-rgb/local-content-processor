"""Pure raw_job_bundle assembly: extracted content + asset outcomes -> Manifest
+ derived crawl status (plan R9 / G2). No network, no Scrapy — so the status
rules are unit-testable in isolation and shared by ingest + the Scrapy path.

R9 / state-machine mapping:
- total extraction failure (neither title nor body) -> CRAWL_FAILED (retriable)
- title OR body missing (exactly one present)        -> needs_revision
- any asset FAILED (but content ok)                  -> CRAWLED_WARN
- everything ok                                       -> CRAWLED
"""

from __future__ import annotations

import hashlib

from ...core.models import (
    AssetRef,
    AssetState,
    Hashes,
    Manifest,
    SourceType,
)
from .base import (
    STATUS_CRAWL_FAILED,
    STATUS_CRAWLED,
    STATUS_CRAWLED_WARN,
    STATUS_NEEDS_REVISION,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def derive_status(
    *,
    title: str | None,
    body: str | None,
    assets: list[AssetRef],
    fatal: bool = False,
) -> str:
    """Pure derivation of the crawl outcome from extracted fields + assets."""
    has_title = bool(title and title.strip())
    has_body = bool(body and body.strip())

    if fatal or (not has_title and not has_body):
        return STATUS_CRAWL_FAILED
    if not has_title or not has_body:
        return STATUS_NEEDS_REVISION
    if any(a.state is AssetState.FAILED for a in assets):
        return STATUS_CRAWLED_WARN
    return STATUS_CRAWLED


def build_manifest(
    *,
    job_id: str,
    source_type: SourceType,
    source_domain: str | None,
    fetched_at: str | None,
    assets: list[AssetRef],
    source_html: str | None,
    source_text: str | None,
    crawl_status: str,
) -> Manifest:
    """Build the per-job Manifest. Stays PII-free: no title/body/url stored,
    only per-asset refs + content hashes (plan: manifest PII-free)."""
    hashes = Hashes(
        source_html_sha256=sha256_text(source_html) if source_html is not None else None,
        source_text_sha256=sha256_text(source_text) if source_text is not None else None,
    )
    return Manifest(
        job_id=job_id,
        source_type=source_type,
        source_domain=source_domain,
        crawl_status=crawl_status,
        fetched_at=fetched_at,
        assets=assets,
        hashes=hashes,
    )
