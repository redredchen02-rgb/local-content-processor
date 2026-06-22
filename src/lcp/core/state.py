"""Job state machine: enum + transition table + pure transition validation.

Pure core layer — no I/O, no framework. The transition table is the single
source of truth for legal job lifecycle moves (plan "Job 狀態機 transition
table"). Freeze (no re-run after a review packet is frozen) is enforced by
EDGE ABSENCE, not a guard: there is intentionally no REVIEW_PENDING->PROCESSING
edge. PROCESSING is transient and must never be a persisted resting state."""

from __future__ import annotations

from enum import Enum

from .errors import InputValidationError


class JobState(str, Enum):
    NEW = "new"
    CRAWLED = "crawled"
    CRAWLED_WARN = "crawled_warn"
    PROCESSING = "processing"
    PROCESS_FAILED = "process_failed"
    CRAWL_FAILED = "crawl_failed"
    PROCESSED = "processed"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    NEEDS_REVISION = "needs_revision"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    PUBLISHED_RECORDED = "published_recorded"


class ReviewReason(str, Enum):
    """Why a job needs a human (single NEEDS_HUMAN_REVIEW state, plan).

    Stored in SQLite as the enum CODE only — never free text — so the index
    stays PII-free."""

    RISK = "risk"
    DEDUP = "dedup"
    GROUNDING = "grounding"


# Transient state that must NOT be written to SQLite (crash detection relies on
# a .processing marker file instead; plan Key Decisions / 架構審查 2c/9).
TRANSIENT_STATES: frozenset[JobState] = frozenset({JobState.PROCESSING})

# Terminal states have no outgoing edges.
#
# BLOCKED and DUPLICATE are deliberately NOT terminal (operator decision,
# 2026-06-18, U8): with false-terminal classifications provably possible, an
# unrecoverable dead-end is the larger harm. They carry a single operator-only
# recovery edge to SUPERSEDED (never automatic; see _TRANSITIONS below).
# SUPERSEDED itself STAYS terminal — recovery never reopens a blocked job in
# place; the only way back into review is a brand-new job re-entering at NEW.
# That asymmetry is what stops the recovery edge from becoming a
# content-laundering path.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {
        JobState.REJECTED,
        JobState.SUPERSEDED,
        JobState.PUBLISHED_RECORDED,
    }
)

# Legal transitions, encoded from the plan transition table. Every side-branch
# has an exit edge; retry and supersede edges are included. The ABSENCE of
# REVIEW_PENDING->PROCESSING is deliberate (freeze via edge-absence).
_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    # A fresh crawl/ingest can land directly in any initial outcome (R9):
    # clean -> CRAWLED, partial assets -> CRAWLED_WARN, missing title/body ->
    # NEEDS_REVISION, total failure -> CRAWL_FAILED.
    JobState.NEW: frozenset(
        {
            JobState.CRAWLED,
            JobState.CRAWLED_WARN,
            JobState.NEEDS_REVISION,
            JobState.CRAWL_FAILED,
        }
    ),
    JobState.CRAWL_FAILED: frozenset({JobState.NEW}),  # retry
    JobState.CRAWLED: frozenset({JobState.CRAWLED_WARN, JobState.PROCESSING}),
    JobState.CRAWLED_WARN: frozenset({JobState.PROCESSING}),  # retry/process
    JobState.PROCESSING: frozenset(
        {
            JobState.PROCESS_FAILED,
            JobState.BLOCKED,
            JobState.DUPLICATE,
            JobState.NEEDS_HUMAN_REVIEW,
            JobState.NEEDS_REVISION,
            JobState.PROCESSED,
        }
    ),
    JobState.PROCESS_FAILED: frozenset({JobState.PROCESSING}),  # retry
    JobState.NEEDS_HUMAN_REVIEW: frozenset(
        {JobState.PROCESSED, JobState.REJECTED, JobState.SUPERSEDED}
    ),
    JobState.NEEDS_REVISION: frozenset(
        {JobState.PROCESSING, JobState.SUPERSEDED}  # re-run in place / supersede
    ),
    JobState.PROCESSED: frozenset({JobState.REVIEW_PENDING}),
    # No REVIEW_PENDING->PROCESSING: freeze is enforced by this edge's absence.
    JobState.REVIEW_PENDING: frozenset({JobState.APPROVED, JobState.REJECTED, JobState.SUPERSEDED}),
    JobState.APPROVED: frozenset({JobState.PUBLISHED_RECORDED, JobState.SUPERSEDED}),
    # Operator-only recovery edge (U8): a false-terminal BLOCKED/DUPLICATE job
    # can be abandoned to SUPERSEDED by an explicit human action (never reached
    # by any automatic path in _process_inner/pipeline). The successor set is
    # EXACTLY {SUPERSEDED} — no edge back to PROCESSING/CRAWLED — so this never
    # reopens the job in place (anti-laundering; SUPERSEDED stays terminal).
    JobState.BLOCKED: frozenset({JobState.SUPERSEDED}),
    JobState.DUPLICATE: frozenset({JobState.SUPERSEDED}),
    # Terminal states (no outgoing edges).
    JobState.REJECTED: frozenset(),
    JobState.SUPERSEDED: frozenset(),
    JobState.PUBLISHED_RECORDED: frozenset(),
}


def allowed_transitions(state: JobState) -> frozenset[JobState]:
    """Successor states reachable from `state` (empty for terminal states)."""
    return _TRANSITIONS[state]


def is_legal_transition(from_state: JobState, to_state: JobState) -> bool:
    """Pure predicate: True iff from_state -> to_state is in the table."""
    return to_state in _TRANSITIONS[from_state]


def validate_transition(from_state: JobState, to_state: JobState) -> None:
    """Raise InputValidationError on an illegal transition; return None if legal.

    Pure judgement — no I/O. Callers in the storage layer use this before
    persisting a new state."""
    if not is_legal_transition(from_state, to_state):
        raise InputValidationError(
            f"illegal job transition: {from_state.value} -> {to_state.value}"
        )
