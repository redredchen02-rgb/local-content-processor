"""Pydantic models: the single source of truth shared by CLI and GUI shells.

Core (pure) layer — no framework, no I/O imports here."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    URL = "url"
    URL_LIST = "url_list"
    LOCAL_DIR = "local_dir"


class AssetKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    TEXT = "text"


class AssetState(str, Enum):
    OK = "ok"
    FAILED = "failed"
    NEEDS_REVISION = "needs_revision"


class AssetRef(BaseModel):
    """One downloaded/ingested asset, with its per-asset outcome (plan G2)."""

    kind: AssetKind
    path: str
    source_url: str | None = None
    sha256: str | None = None
    state: AssetState = AssetState.OK
    note: str | None = None


class Hashes(BaseModel):
    source_html_sha256: str | None = None
    source_text_sha256: str | None = None


class Manifest(BaseModel):
    """Per-job manifest. PII-free index fields live in SQLite; the manifest
    itself stays PII-free (no scraped title/body/url-with-PII)."""

    job_id: str
    source_type: SourceType
    source_domain: str | None = None
    crawl_status: str = "pending"
    fetched_at: str | None = None
    assets: list[AssetRef] = Field(default_factory=list)
    hashes: Hashes = Field(default_factory=Hashes)
    logic_version: str = "0.1.0"
