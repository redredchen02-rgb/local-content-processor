"""Shared persist helper for Stage-2 gate adapters.

Why this exists (the PROCESSING seam): the state machine reaches the gate
resting states (BLOCKED / DUPLICATE / NEEDS_HUMAN_REVIEW) ONLY from PROCESSING,
and PROCESSING is transient — it is never written to SQLite (Unit 3). So the
persisted predecessor of a gate write is CRAWLED / CRAWLED_WARN with a
``.processing`` marker standing in for the in-memory PROCESSING state.

``JobStore.set_state`` validates ``persisted_current -> new`` and so cannot
express ``PROCESSING -> target`` (PROCESSING can't be the persisted current).
This helper bridges that seam WITHOUT modifying Unit 3: it validates the
canonical ``persisted_current -> PROCESSING -> target`` edge via the pure state
machine, persists the resting state through the same PII-free SQLite update
JobStore uses, then clears the ``.processing`` marker. All judgement still flows
through ``core/state.validate_transition``.

CONTRACT (caller-owned marker): this seam does NOT require the ``.processing``
marker to be present, and it does not assert on it. The marker is the CALLER's
to set — ``Pipeline.process`` drops it at Stage-2 entry — and this seam only
clears it (idempotently) after the resting state is committed. Adding a
marker-present assertion here would (a) pull filesystem marker I/O back under the
WAL write lock (the PR #8 constraint forbids holding the lock across file I/O)
and (b) break the legitimate ``PROCESS_FAILED`` retry path, which re-enters
without the original marker. The marker's real consumer is
``Pipeline.reconcile`` (U7), which reads it at the worklist boundary.
"""

from __future__ import annotations

from ...core.state import JobState, ReviewReason
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

    Thin processor-facing wrapper: the SQL + ``PROCESSING -> target`` validation
    + ``.processing`` marker dance now live in :meth:`JobStore.persist_from_
    processing` (single connection, JobStore owns its own schema). Kept as the
    name the gate adapters import."""
    return store.persist_from_processing(
        job_id,
        target,
        updated_at=updated_at,
        review_reason=review_reason,
        error_code=error_code,
    )
