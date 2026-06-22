"""Gossip batch-injection adapter (plan 001, Unit 4).

Bridges the standalone gossip_scraper (Stage 1) to lcp: turns a list of
GossipItem dicts into one lcp job each, persisting each job's source URL to a
job-dir file so lcp's existing crawler can deep-crawl it later by job_id.

The source URL is deliberately kept OUT of the PII-free SQLite index — it lives
in the PII-bearing job bundle (``data/jobs/<id>/source.json``, 0600), exactly
where other per-job material lives. This resolves the ingest→crawl gap without
weakening the index invariant.

Validation here is cheap scheme-only (the crawler re-checks DNS ``is_global`` at
crawl time); empty/invalid URLs and malformed items are skipped with a reason
rather than failing the whole batch, and the skip report is non-lossy. An
oversized batch is refused outright so a gamed/huge hot-list cannot fan out into
unbounded jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ...core.errors import InputValidationError
from ..crawler.net_guard import ALLOWED_SCHEMES
from ._fs import atomic_write_0600 as _atomic_write_0600
from .job_store import JobStore

SOURCE_NAME = "source.json"
DEFAULT_MAX_ITEMS = 200


@dataclass
class IngestReport:
    """Outcome of a batch ingest: created job ids + non-lossy skip records."""

    created: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "created": list(self.created),
            "skipped": list(self.skipped),
            "created_count": len(self.created),
            "skipped_count": len(self.skipped),
        }


def parse_payload(raw: str) -> list[dict[str, object]]:
    """Parse a GossipItem JSON payload into a list of dicts.

    Raises InputValidationError on non-JSON, a non-array root, or a non-object
    element (the most common malformed inputs)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise InputValidationError(f"gossip payload is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise InputValidationError("gossip payload must be a JSON array")
    items: list[dict[str, object]] = []
    for i, elem in enumerate(data):
        if not isinstance(elem, dict):
            raise InputValidationError(f"gossip item {i} is not a JSON object")
        items.append(elem)
    return items


def make_job_id(platform: str, url: str) -> str:
    """Deterministic job id from platform + url, so re-ingesting the same item
    maps to the same job (``create_job`` then refuses it as a duplicate, which
    the caller reports as ``already_exists`` — idempotent re-ingest)."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    safe_platform = "".join(ch for ch in platform if ch.isalnum()) or "x"
    return f"gossip-{safe_platform}-{digest}"


def _valid_scheme(url: str) -> bool:
    """Cheap scheme allowlist check (http/https) — no DNS. The crawler re-checks
    DNS is_global at crawl time."""
    if not url:
        return False
    try:
        return urlparse(url).scheme.lower() in ALLOWED_SCHEMES
    except ValueError:
        return False


def write_source(job_dir: Path, *, url: str, platform: str, title: str) -> None:
    """Persist the source URL (+ provenance) to ``<job_dir>/source.json``
    atomically (0600) — the per-job home for the URL the deferred crawl reads.

    Reuses the storage layer's shared atomic-0600 writer so the crash-safety /
    permission invariant lives in exactly one place."""
    payload = json.dumps(
        {"url": url, "platform": platform, "title": title},
        ensure_ascii=False,
    )
    _atomic_write_0600(job_dir / SOURCE_NAME, payload)


def read_source_url(job_dir: Path) -> str | None:
    """Read the persisted source URL from ``<job_dir>/source.json``; return None
    if the job was not gossip-ingested or the file is missing/malformed."""
    path = job_dir / SOURCE_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError on a non-UTF-8 / corrupt file —
        # honor the "malformed -> None" contract for every corruption mode.
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    return url if isinstance(url, str) and url else None


def ingest_items(
    items: list[dict[str, object]],
    store: JobStore,
    *,
    ts: str,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> IngestReport:
    """Create one NEW job per valid item (persisting its source URL).

    Fails open per item (skip + reason, non-lossy report); refuses an oversized
    batch outright. source.json is written before ``create_job`` so a failed
    create never leaves an orphan DB row without its URL."""
    if len(items) > max_items:
        raise InputValidationError(
            f"gossip batch too large: {len(items)} items > max_items {max_items}"
        )
    report = IngestReport()
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        platform = str(item.get("platform") or "").strip()
        title = str(item.get("title") or "").strip()
        if not _valid_scheme(url):
            report.skipped.append({"reason": "invalid_or_empty_url", "url": url, "title": title})
            continue
        if not platform or not title:
            report.skipped.append({"reason": "missing_fields", "url": url, "title": title})
            continue
        if url in seen:
            report.skipped.append({"reason": "duplicate_in_batch", "url": url, "title": title})
            continue
        seen.add(url)
        job_id = make_job_id(platform, url)
        store.ensure_job_dir(job_id)
        write_source(store.job_dir(job_id), url=url, platform=platform, title=title)
        try:
            store.create_job(job_id, created_at=ts)
        except InputValidationError:
            report.skipped.append(
                {
                    "reason": "already_exists",
                    "url": url,
                    "title": title,
                    "job_id": job_id,
                }
            )
            continue
        report.created.append(job_id)
    return report
