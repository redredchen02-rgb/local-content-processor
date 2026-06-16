"""SQLite job index + folder-per-job blob layout + best-effort deletion.

PII-FREE BY CONSTRUCTION: the jobs table holds ONLY low-risk index columns —
job_id, state, timestamps, content hashes, error_code, and review_reason as an
ENUM CODE (never free text). Prohibited columns (title / body / source URL /
author / domain / review_reason text) never touch SQLite, so no secure_delete
is needed for the index.

PROCESSING is transient and is NEVER written to SQLite (plan 架構審查 2c/9);
crash detection relies on a .processing marker file in the job dir instead.

Concurrency: WAL mode + one fresh connection per call + busy_timeout, so the
CLI, GUI, and background threads can read/write concurrently without
corruption (plan: WAL + 每執行緒/進程獨立連線 + busy_timeout).

Plaintext at rest: job blobs are 0600 plaintext (no encryption — that is
post-MVP, plan R42). Deletion is BEST-EFFORT unlink/rmtree only; we do NOT and
MUST NOT claim cryptographic erasure on SSDs."""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ...core.errors import InputValidationError
from ...core.state import JobState, ReviewReason, TRANSIENT_STATES, validate_transition
from .audit_log import EVENT_ERASURE, AuditLog

DB_NAME = "lcp.db"
PROCESSING_MARKER = ".processing"
_BUSY_TIMEOUT_MS = 5000

# Allowed index columns only — see module docstring / pii-inventory.md.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_html_sha256 TEXT,
    source_text_sha256 TEXT,
    error_code TEXT,
    review_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    state: JobState
    created_at: str
    updated_at: str
    source_html_sha256: str | None = None
    source_text_sha256: str | None = None
    error_code: str | None = None
    review_reason: ReviewReason | None = None


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        state=JobState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_html_sha256=row["source_html_sha256"],
        source_text_sha256=row["source_text_sha256"],
        error_code=row["error_code"],
        review_reason=(
            ReviewReason(row["review_reason"]) if row["review_reason"] else None
        ),
    )


class BestEffortDeletionResult:
    """Result of a job deletion. Names make the honest limitation explicit:
    files were unlinked best-effort; this is NOT cryptographic erasure (SSD
    wear-leveling can retain copies). Surfaced so callers/UI can label it
    truthfully (plan: best-effort, 不宣稱抹除)."""

    cryptographic_erasure = False  # honest: we never claim crypto-shredding

    def __init__(self, job_id: str, removed: bool):
        self.job_id = job_id
        self.removed = removed
        self.method = "best_effort_unlink"

    def __repr__(self) -> str:
        return (
            f"BestEffortDeletionResult(job_id={self.job_id!r}, removed={self.removed}, "
            f"method={self.method!r}, cryptographic_erasure=False)"
        )


class JobStore:
    """SQLite index + folder-per-job blob storage under base_dir/."""

    def __init__(self, base_dir: str | os.PathLike[str] = "./data"):
        self.base_dir = Path(base_dir)
        self.jobs_root = self.base_dir / "jobs"
        self.db_path = self.base_dir / DB_NAME
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # --- connection management: one fresh connection per call/thread ---

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # --- job directory layout: data/jobs/<job_id>/{raw,processed,review}/ ---

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def ensure_job_dir(self, job_id: str) -> Path:
        d = self.job_dir(job_id)
        for sub in ("raw", "processed", "review"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        # Explicit 0700 in case the process umask was relaxed elsewhere.
        for p in (d, d / "raw", d / "processed", d / "review"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        return d

    # --- CRUD ---

    def create_job(
        self,
        job_id: str,
        *,
        created_at: str,
        state: JobState = JobState.NEW,
    ) -> JobRecord:
        if state in TRANSIENT_STATES:
            raise InputValidationError(
                f"cannot persist transient state {state.value} to SQLite"
            )
        self.ensure_job_dir(job_id)
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (job_id, state.value, created_at, created_at),
                )
            except sqlite3.IntegrityError as e:
                raise InputValidationError(
                    f"job already exists: {job_id}"
                ) from e
            conn.commit()
        finally:
            conn.close()
        return JobRecord(
            job_id=job_id, state=state, created_at=created_at, updated_at=created_at
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_record(row) if row else None

    def set_state(
        self,
        job_id: str,
        new_state: JobState,
        *,
        updated_at: str,
        error_code: str | None = None,
        review_reason: ReviewReason | None = None,
    ) -> JobRecord:
        """Validate the transition via core/state.py, then persist.

        PROCESSING is transient: we do NOT write it to SQLite. Callers move the
        in-memory state through PROCESSING but only persist a resting state
        (and drop a .processing marker file via mark_processing())."""
        current = self.get_job(job_id)
        if current is None:
            raise InputValidationError(f"unknown job: {job_id}")
        validate_transition(current.state, new_state)  # raises if illegal
        if new_state in TRANSIENT_STATES:
            raise InputValidationError(
                f"cannot persist transient state {new_state.value} to SQLite; "
                "use mark_processing() for the .processing marker instead"
            )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, error_code = ?, "
                "review_reason = ? WHERE job_id = ?",
                (
                    new_state.value,
                    updated_at,
                    error_code,
                    review_reason.value if review_reason else None,
                    job_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return JobRecord(
            job_id=job_id,
            state=new_state,
            created_at=current.created_at,
            updated_at=updated_at,
            source_html_sha256=current.source_html_sha256,
            source_text_sha256=current.source_text_sha256,
            error_code=error_code,
            review_reason=review_reason,
        )

    def set_hashes(
        self,
        job_id: str,
        *,
        updated_at: str,
        source_html_sha256: str | None = None,
        source_text_sha256: str | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE jobs SET source_html_sha256 = COALESCE(?, source_html_sha256), "
                "source_text_sha256 = COALESCE(?, source_text_sha256), updated_at = ? "
                "WHERE job_id = ?",
                (source_html_sha256, source_text_sha256, updated_at, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def list_by_state(self, state: JobState) -> list[JobRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created_at",
                (state.value,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_record(r) for r in rows]

    # --- transient PROCESSING marker (NOT persisted in SQLite) ---

    def mark_processing(self, job_id: str) -> Path:
        marker = self.job_dir(job_id) / PROCESSING_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return marker

    def clear_processing(self, job_id: str) -> None:
        marker = self.job_dir(job_id) / PROCESSING_MARKER
        if marker.exists():
            marker.unlink()

    def is_processing(self, job_id: str) -> bool:
        return (self.job_dir(job_id) / PROCESSING_MARKER).exists()

    # --- best-effort deletion (NOT cryptographic erasure) ---

    def delete_job(
        self,
        job_id: str,
        *,
        ts: str,
        actor: str,
        audit: AuditLog | None = None,
    ) -> BestEffortDeletionResult:
        """Best-effort delete: rmtree the job dir + remove the SQLite row, and
        record an ERASURE audit event. We do NOT claim cryptographic erasure —
        SSD wear-leveling may retain copies (plan R42). The returned result's
        cryptographic_erasure flag is always False.

        The ERASURE event is recorded BEFORE rmtree so that, with the default
        layout where audit.jsonl lives inside the job dir, the event is written
        and then removed together with the dir; an external audit log survives
        the deletion and still verifies."""
        if audit is not None:
            audit.append(
                ts=ts,
                stage="storage",
                event=EVENT_ERASURE,
                job_id=job_id,
                actor=actor,
                extra={"method": "best_effort_unlink", "cryptographic_erasure": False},
            )
        d = self.job_dir(job_id)
        removed = False
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed = not d.exists()
        conn = self._connect()
        try:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.commit()
        finally:
            conn.close()
        return BestEffortDeletionResult(job_id=job_id, removed=removed)
