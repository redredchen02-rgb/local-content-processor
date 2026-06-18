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
# Per-job-dir crash-attempt counter (U7). A FILE, never a jobs-table column: the
# jobs schema is PII-free-by-construction (see module docstring) and adding a
# count column would collide with that tripwire. Presence-only like .processing,
# but carries a small integer (no PII) so a DETERMINISTIC crash surfaces to a
# human after N reconcile passes instead of looping retry->crash->retry forever.
INTERRUPT_COUNT_MARKER = ".interrupt_count"
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


def _chmod_db_0600(db_path: Path) -> None:
    """Force lcp.db (and its WAL/SHM sidecars) to 0600 after init.

    Defense-in-depth: apply_hardening()'s 0o077 umask already yields 0600, but
    this store SHARES lcp.db with the plaintext-PII saved_sources table, so we
    do not want correctness to depend on an entry point remembering the umask.
    The -wal/-shm sidecars carry the same plaintext page data, so tighten them
    too when present. Best-effort: a chmod failure must not break the store."""
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


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


def _is_within(path: Path, ancestor: Path) -> bool:
    """True if `path` is `ancestor` or lives under it (resolved, so `..` cannot
    smuggle a path out). Used to detect an in-job-dir audit log so the confirming
    erasure event does not resurrect a just-deleted job dir."""
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False


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
        # busy_timeout is per-connection, so it MUST be set here. journal_mode=WAL
        # is a PERSISTENT database property (stored in the file header) set once in
        # _init_db — re-issuing it on every connection was a wasted round-trip.
        # The schema has no foreign keys, so PRAGMA foreign_keys was a no-op.
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # persistent; set once
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _chmod_db_0600(self.db_path)

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
        (and drop a .processing marker file via mark_processing()).

        Read + validate + update run in ONE connection under BEGIN IMMEDIATE, so
        the write lock is held across the whole read->validate->update: a
        concurrent writer cannot land a transition between our read and our write
        (closes the read->update race the prior two-connection version had).
        isolation_level=None disables sqlite3's legacy implicit-BEGIN layer so our
        explicit BEGIN IMMEDIATE is the sole transaction control (relying on the
        legacy layer suppressing its own BEGIN is version-fragile)."""
        conn = self._connect()
        conn.isolation_level = None  # manual transaction control (see docstring)
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the WAL write lock before reading
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise InputValidationError(f"unknown job: {job_id}")
            current = _row_to_record(row)
            validate_transition(current.state, new_state)  # raises if illegal
            if new_state in TRANSIENT_STATES:
                raise InputValidationError(
                    f"cannot persist transient state {new_state.value} to SQLite; "
                    "use mark_processing() for the .processing marker instead"
                )
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
            conn.execute("COMMIT")
        finally:
            conn.close()  # rolls back if we raised before COMMIT
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

    def persist_crawl_result(
        self,
        job_id: str,
        target: JobState,
        *,
        updated_at: str,
        source_html_sha256: str | None = None,
        source_text_sha256: str | None = None,
    ) -> JobRecord:
        """Land a Stage-1 outcome — the state transition AND the source hashes —
        in ONE transaction.

        Stage 1 used to write the hashes then the state transition as two SEPARATE
        committed transactions: a crash between them left a job at NEW WITH hashes
        or at CRAWLED WITHOUT hashes (a torn write), and the hash write mutated the
        row even when the subsequent state validation would reject the transition
        (partial mutation on an illegal re-crawl). Folding both writes under one
        ``BEGIN IMMEDIATE`` makes ``(state, hashes)`` atomic: either both land or
        neither does.

        Read + validate + update run under ``BEGIN IMMEDIATE``
        (``isolation_level=None``) so the write lock is held across the whole
        read->validate->update, mirroring ``set_state``. Validates ``current ->
        target`` via the canonical state machine (so an illegal predecessor refuses
        BEFORE any mutation — there is no longer a half-written hash to clean up),
        and refuses a transient target. Hashes use COALESCE so a None leaves the
        existing value intact."""
        if target in TRANSIENT_STATES:
            raise InputValidationError(
                f"cannot persist transient state {target.value} to SQLite"
            )
        conn = self._connect()
        conn.isolation_level = None  # manual transaction control (mirrors set_state)
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the WAL write lock before reading
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise InputValidationError(f"unknown job: {job_id}")
            current = _row_to_record(row)
            validate_transition(current.state, target)  # raises if illegal
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, "
                "source_html_sha256 = COALESCE(?, source_html_sha256), "
                "source_text_sha256 = COALESCE(?, source_text_sha256) "
                "WHERE job_id = ?",
                (
                    target.value,
                    updated_at,
                    source_html_sha256,
                    source_text_sha256,
                    job_id,
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()  # rolls back if we raised before COMMIT
        return JobRecord(
            job_id=job_id,
            state=target,
            created_at=current.created_at,
            updated_at=updated_at,
            source_html_sha256=(
                source_html_sha256
                if source_html_sha256 is not None
                else current.source_html_sha256
            ),
            source_text_sha256=(
                source_text_sha256
                if source_text_sha256 is not None
                else current.source_text_sha256
            ),
            error_code=current.error_code,
            review_reason=current.review_reason,
        )

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

    def list_all(self) -> list[JobRecord]:
        """All persisted jobs in ONE connection, ordered by (created_at, job_id).

        PROCESSING is never persisted, so transient states never appear. Replaces
        the per-state fan-out (one connection per JobState) the unfiltered
        worklist used to do."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at, job_id"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_record(r) for r in rows]

    def counts_by_state(self) -> dict[str, int]:
        """Counts-by-state in ONE connection (GROUP BY) for the batch summary.

        Returns {state_value: count}. PROCESSING is never persisted, so it never
        appears. Replaces the per-state fan-out (one COUNT connection per state)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall()
        finally:
            conn.close()
        return {r["state"]: r["n"] for r in rows}

    def persist_from_processing(
        self,
        job_id: str,
        target: JobState,
        *,
        updated_at: str,
        review_reason: ReviewReason | None = None,
        error_code: str | None = None,
    ) -> JobRecord:
        """Persist a Stage-2 gate's resting state as if from the transient
        PROCESSING state, in a SINGLE connection (read + update).

        This owns the SQL so processor adapters never reach into a private
        connection. Read + validate + update run under ``BEGIN IMMEDIATE``
        (``isolation_level=None``) so the read->update is atomic — a competing
        writer cannot land a transition between our read and our write (same race
        closure as ``set_state``). Validates ``persisted_current -> PROCESSING ->
        target`` via the canonical state machine, refuses a transient target, and
        clears the ``.processing`` marker AFTER the commit.

        The marker is the CALLER's to set (Pipeline.process drops it at Stage-2
        entry); this method does NOT require or assert the marker — it only clears
        it (idempotently) once the resting state is committed. Not asserting is
        deliberate: a marker-present check here would re-pull filesystem I/O under
        the WAL write lock and break the legitimate PROCESS_FAILED retry path,
        which re-enters without the original marker. Marker filesystem I/O
        therefore stays OUTSIDE the write transaction, so the WAL
        write lock is never held across a touch()/mkdir."""
        if target in TRANSIENT_STATES:
            raise InputValidationError(
                f"cannot persist transient state {target.value} to SQLite"
            )
        conn = self._connect()
        conn.isolation_level = None  # manual transaction control (mirrors set_state)
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the WAL write lock before reading
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise InputValidationError(f"unknown job: {job_id}")
            current = _row_to_record(row)
            # Persisted predecessor must legally reach PROCESSING (job is genuinely
            # mid Stage-2), and PROCESSING must legally reach the target.
            validate_transition(current.state, JobState.PROCESSING)
            validate_transition(JobState.PROCESSING, target)
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, error_code = ?, "
                "review_reason = ? WHERE job_id = ?",
                (
                    target.value,
                    updated_at,
                    error_code,
                    review_reason.value if review_reason else None,
                    job_id,
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()  # rolls back if we raised before COMMIT
        # Marker handling stays outside the DB transaction (filesystem I/O must not
        # be held under the WAL write lock); the resting state is already committed.
        self.clear_processing(job_id)
        return JobRecord(
            job_id=job_id,
            state=target,
            created_at=current.created_at,
            updated_at=updated_at,
            source_html_sha256=current.source_html_sha256,
            source_text_sha256=current.source_text_sha256,
            error_code=error_code,
            review_reason=review_reason,
        )

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

    # --- crash-attempt counter (per-job-dir FILE, NOT a SQLite column) ---

    def read_interrupt_count(self, job_id: str) -> int:
        """Crash-attempt count for `job_id` (0 if absent/unreadable/corrupt).

        Fail-safe: a missing, half-written, or garbage counter file reads as 0 so
        reconciliation never crashes on the very artifact it manages."""
        counter = self.job_dir(job_id) / INTERRUPT_COUNT_MARKER
        try:
            return int(counter.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def bump_interrupt_count(self, job_id: str) -> int:
        """Increment and persist the crash-attempt count; return the new value.

        Atomic write (temp-in-same-dir + fsync + 0600 + os.replace) so a crash
        mid-bump can never leave a half-written count that a later read would
        mistake for a different value — it reads back as 0 (fail-safe) at worst."""
        new = self.read_interrupt_count(job_id) + 1
        d = self.job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / INTERRUPT_COUNT_MARKER
        tmp = path.with_name(f".{INTERRUPT_COUNT_MARKER}.tmp.{os.getpid()}")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(str(new))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)  # PII-at-rest discipline (no group/other access)
            os.replace(tmp, path)  # atomic
        finally:
            if tmp.exists():
                tmp.unlink()
        return new

    def clear_interrupt_count(self, job_id: str) -> None:
        """Reset the counter (a clean re-process starts the loop guard fresh)."""
        counter = self.job_dir(job_id) / INTERRUPT_COUNT_MARKER
        if counter.exists():
            counter.unlink()

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
        record ERASURE audit events. We do NOT claim cryptographic erasure —
        SSD wear-leveling may retain copies (plan R42). The returned result's
        cryptographic_erasure flag is always False.

        Two-event design so the audit can never assert an erasure that did not
        happen (U10). The FIRST event (intent) is recorded BEFORE rmtree so that,
        with the default layout where audit.jsonl lives inside the job dir, the
        event is written and then removed together with the dir; an external audit
        log survives the deletion and still verifies. Because it is written before
        the blob removal, it cannot know the real outcome — so a SECOND confirming
        event records the TRUE result (removed / rows_deleted / dir_existed) after
        the writes complete. Without it, a stuck/permission-denied file would leave
        the tamper-evident log asserting 'erased' while reality (and the returned
        removed=False) say otherwise — a compliance-relevant mismatch.

        rmtree runs WITHOUT ignore_errors so a stuck file is detected and reported,
        not silently swallowed: removed reflects whether the dir actually went away."""
        d = self.job_dir(job_id)
        dir_existed = d.exists()
        if audit is not None:
            # Intent record. Survives even if the blob removal below fails.
            audit.append(
                ts=ts,
                stage="storage",
                event=EVENT_ERASURE,
                job_id=job_id,
                actor=actor,
                extra={"method": "best_effort_unlink", "cryptographic_erasure": False},
            )
        if dir_existed:
            try:
                shutil.rmtree(d)
            except OSError:
                # A held/permission-denied file: detect-and-report rather than the
                # old ignore_errors swallow. removed below will be False (the dir
                # still exists), keeping the reported outcome truthful.
                pass
        # removed == "there was a job dir and it is now gone". An unknown job
        # (no dir) reports removed=False — "nothing was erased" — never a vacuous
        # True off an empty path, and never True while a stuck file survives.
        removed = dir_existed and not d.exists()

        # Delete the row under BEGIN IMMEDIATE + isolation_level=None, uniform with
        # set_state/persist_crawl_result, so a future read-then-write edit here
        # cannot silently reintroduce the read->update race PR #8 closed.
        conn = self._connect()
        conn.isolation_level = None  # manual transaction control (mirrors set_state)
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the WAL write lock
            cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            rows_deleted = cur.rowcount if cur.rowcount is not None else 0
            conn.execute("COMMIT")
        finally:
            conn.close()  # rolls back if we raised before COMMIT

        # Confirming record: the TRUE outcome (PII-free codes/bools/counts). Skip
        # it when the audit log lives INSIDE the job dir we just removed — writing
        # it there would resurrect the deleted dir, and that in-dir log was
        # deliberately sacrificed with the blobs (the intent record above is its
        # surviving trace). The external-log layout (production: audit at the
        # storage root) is where a compliance reader needs the truthful outcome.
        if audit is not None and not _is_within(audit.path, d):
            audit.append(
                ts=ts,
                stage="storage",
                event=EVENT_ERASURE,
                job_id=job_id,
                actor=actor,
                extra={
                    "method": "best_effort_unlink",
                    "cryptographic_erasure": False,
                    "dir_existed": dir_existed,
                    "removed": removed,
                    "rows_deleted": rows_deleted,
                },
            )
        return BestEffortDeletionResult(job_id=job_id, removed=removed)
