"""Shared persist helper for Stage-2 gate adapters.

Why this exists (the PROCESSING seam): the state machine reaches the gate
resting states (BLOCKED / DUPLICATE / NEEDS_HUMAN_REVIEW) ONLY from PROCESSING,
and PROCESSING is transient — it is never written to SQLite (Unit 3). So the
persisted predecessor of a gate write is CRAWLED / CRAWLED_WARN with a
``.processing`` marker standing in for the in-memory PROCESSING state.

``JobStore.set_state`` validates ``persisted_current -> new`` and so cannot
express ``PROCESSING -> target`` (PROCESSING can't be the persisted current).
This helper bridges that seam WITHOUT modifying Unit 3: it requires the
``.processing`` marker (evidence the job is mid-processing), validates the
canonical ``PROCESSING -> target`` edge via the pure state machine, persists the
resting state through the same PII-free SQLite update JobStore uses, then clears
the marker. All judgement still flows through ``core/state.validate_transition``.
"""

from __future__ import annotations

from ...core.errors import InputValidationError
from ...core.state import JobState, ReviewReason, validate_transition
from ..storage.job_store import JobRecord, JobStore


def persist_gate_state(
    store: JobStore,
    job_id: str,
    target: JobState,
    *,
    updated_at: str,
    review_reason: ReviewReason | None = None,
    error_code: str | None = None,
) -> JobRecord:
    """Persist a Stage-2 gate's resting state as if from PROCESSING.

    Validates ``PROCESSING -> target`` with the canonical state machine (so
    illegal targets still raise), then writes the resting state + review_reason
    through a fresh JobStore connection (WAL, busy_timeout — same as Unit 3).
    The job must currently rest at a legal PROCESSING-predecessor (CRAWLED /
    CRAWLED_WARN); otherwise we refuse, to keep the lifecycle honest."""
    current = store.get_job(job_id)
    if current is None:
        raise InputValidationError(f"unknown job: {job_id}")
    # The persisted predecessor must legally reach PROCESSING (i.e. the job is
    # genuinely mid Stage-2), and PROCESSING must legally reach the target.
    validate_transition(current.state, JobState.PROCESSING)
    validate_transition(JobState.PROCESSING, target)

    store.mark_processing(job_id)
    conn = store._connect()
    try:
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
        conn.commit()
    finally:
        conn.close()
    store.clear_processing(job_id)
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
