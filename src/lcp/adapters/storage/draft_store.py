"""Persisted Stage-2 draft I/O: draft.json read/write + the raw source.txt read.

These were defined in pipeline.py (the ORCHESTRATOR) and imported UPWARD by the
publisher (signoff) — an adapter reaching into the orchestration layer (a latent
publisher->orchestrator cycle). They are plain folder-per-job storage operations,
so they belong in the storage layer. pipeline re-exports them (``pl.load_draft``
/ ``pl.save_draft``) so the shells are unchanged, and signoff now imports them
here (publisher -> storage, a normal downward dependency)."""

from __future__ import annotations

from pathlib import Path

from ...core.draft import Draft
from ._fs import atomic_write_0600
from .job_store import JobStore

_DRAFT_NAME = "draft.json"


def _draft_path(store: JobStore, job_id: str) -> Path:
    return store.job_dir(job_id) / "processed" / _DRAFT_NAME


def _read_source_text(store: JobStore, job_id: str) -> str:
    """Read the scraped/ingested body text from the raw bundle (source.txt).

    Local string read only — never parses or fetches a URL (R41)."""
    path = store.job_dir(job_id) / "raw" / "source.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def save_draft(store: JobStore, job_id: str, draft: Draft) -> Path:
    """Persist the assembled draft to data/jobs/<id>/processed/draft.json (0600).

    This is what the review-packet command reads back to FREEZE — so the freeze
    binds the exact draft Stage 2 produced, not a re-assembled (and therefore
    non-deterministic) one. Plaintext 0600, best-effort deletion (R42).

    Uses the shared :func:`atomic_write_0600` (mkstemp + fsync + chmod 0600 +
    os.replace) so the committed draft is never world-readable and a crash
    mid-write never leaves a torn draft.json."""
    path = _draft_path(store, job_id)
    atomic_write_0600(path, draft.model_dump_json(indent=2))
    return path


def load_draft(store: JobStore, job_id: str) -> Draft | None:
    """Read back the persisted Stage-2 draft, or None if it was never produced."""
    path = _draft_path(store, job_id)
    if not path.exists():
        return None
    return Draft.model_validate_json(path.read_text(encoding="utf-8"))
