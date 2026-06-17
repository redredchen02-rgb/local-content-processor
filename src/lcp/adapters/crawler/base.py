"""Crawler abstract contract (plan Key Decisions / 架構審查 7).

The seam that lets a post-MVP Playwright (or a back-office API client) be
plugged in WITHOUT a rewrite: every crawler takes a `SourceSpec` (what to
fetch + where to write) and returns a `RawJobBundle` (a manifest-like result +
per-asset statuses + a derived job status). Scrapy is the FIRST implementation,
not the only shape.

`Crawler` is both an ABC and runtime-checkable Protocol so a fake in-memory
impl and the Scrapy impl satisfy the same contract, and tests can prove the
seam is real (a fake works wherever the Scrapy impl would).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ...core.models import AssetRef, Manifest, SourceType


@dataclass(frozen=True)
class SourceSpec:
    """Crawler input: WHAT to fetch and WHERE to write the raw bundle.

    For URL/URL_LIST sources `url` / `urls` apply; for LOCAL_DIR `local_dir`
    is the material folder. `job_dir` is the per-job directory (its `raw/`
    subdir receives source.{html,txt}, metadata.json, images/, videos/)."""

    job_id: str
    source_type: SourceType
    job_dir: Path
    url: str | None = None
    urls: list[str] = field(default_factory=list)
    local_dir: Path | None = None
    max_assets: int = 100


@dataclass(frozen=True)
class RawJobBundle:
    """Crawler output: the produced raw bundle, described.

    `manifest` is the persisted Manifest (with per-asset AssetRef states);
    `job_status` is the derived crawl outcome string the caller maps onto the
    JobState machine (e.g. "crawled" / "crawled_warn" / "crawl_failed" /
    "needs_revision"); `raw_dir` is data/jobs/<id>/raw."""

    job_id: str
    raw_dir: Path
    manifest: Manifest
    job_status: str
    notes: list[str] = field(default_factory=list)

    @property
    def assets(self) -> list[AssetRef]:
        return self.manifest.assets


# Derived crawl outcomes (map onto JobState in the runner/caller).
STATUS_CRAWLED = "crawled"
STATUS_CRAWLED_WARN = "crawled_warn"
STATUS_NEEDS_REVISION = "needs_revision"
STATUS_CRAWL_FAILED = "crawl_failed"


@runtime_checkable
class CrawlerProtocol(Protocol):
    """Structural contract — anything with this `crawl` signature qualifies."""

    def crawl(self, spec: SourceSpec) -> RawJobBundle: ...


class Crawler(ABC):
    """Nominal base class for crawler implementations (Scrapy, ingest, fakes)."""

    @abstractmethod
    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        """Fetch/ingest `spec` and produce a RawJobBundle. MUST NOT clobber an
        existing raw bundle (delegate to manifest write_manifest create_only)."""
        raise NotImplementedError
