"""Per-job manifest persistence: atomic commit + content-hash idempotency.

Atomic write = temp file in the SAME directory + os.replace (rename is atomic
on POSIX, so a crash mid-write never leaves a half-written manifest.json).

Content-hash idempotency (skip-if-unchanged) is for DETERMINISTIC stages ONLY:
we sha256 the serialized content and skip rewriting when it is identical to
what is already on disk. Non-deterministic output (LLM drafts) MUST NOT use
this — it is frozen after entering REVIEW_PENDING instead (plan: checksum 冪等
只適用確定性 stage)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ...core.errors import InputValidationError
from ...core.models import Manifest

MANIFEST_NAME = "manifest.json"


def _serialize(manifest: Manifest) -> str:
    """Deterministic JSON so identical content yields identical bytes/hash."""
    return json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )


def content_hash(manifest: Manifest) -> str:
    return hashlib.sha256(_serialize(manifest).encode("utf-8")).hexdigest()


def manifest_path(job_dir: str | os.PathLike[str]) -> Path:
    return Path(job_dir) / MANIFEST_NAME


def _atomic_write(path: Path, text: str) -> None:
    """temp file in the same dir + fsync + os.replace (atomic on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic
    finally:
        if tmp.exists():
            tmp.unlink()


def read_manifest(job_dir: str | os.PathLike[str]) -> Manifest | None:
    path = manifest_path(job_dir)
    if not path.exists():
        return None
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_manifest(
    job_dir: str | os.PathLike[str],
    manifest: Manifest,
    *,
    create_only: bool = False,
    deterministic_skip: bool = False,
) -> bool:
    """Atomically write the manifest. Returns True if written, False if skipped.

    create_only=True: refuse to overwrite an existing manifest (plan R11 — do
    not clobber an existing job's raw bundle).

    deterministic_skip=True: content-hash idempotency for deterministic stages
    — if the on-disk manifest serializes to the same bytes, skip the write and
    return False. Do NOT enable this for manifests carrying non-deterministic
    LLM output."""
    path = manifest_path(job_dir)
    if create_only and path.exists():
        raise InputValidationError(
            f"refusing to overwrite existing manifest: {path}"
        )
    text = _serialize(manifest)
    if deterministic_skip and path.exists():
        # Both sides are the same deterministic serialization; compare the
        # strings directly (hashing both just to compare was redundant).
        if path.read_text(encoding="utf-8") == text:
            return False  # unchanged deterministic content -> idempotent skip
    _atomic_write(path, text)
    return True
