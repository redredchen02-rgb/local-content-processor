import pytest

from lcp.core.errors import InputValidationError
from lcp.core.state import (
    JobState,
    ReviewReason,
    TERMINAL_STATES,
    TRANSIENT_STATES,
    allowed_transitions,
    is_legal_transition,
    validate_transition,
)


def test_full_happy_path_all_legal():
    path = [
        JobState.NEW,
        JobState.CRAWLED,
        JobState.PROCESSING,
        JobState.PROCESSED,
        JobState.REVIEW_PENDING,
        JobState.APPROVED,
        JobState.PUBLISHED_RECORDED,
    ]
    for a, b in zip(path, path[1:]):
        validate_transition(a, b)  # must not raise
        assert is_legal_transition(a, b)


def test_illegal_transition_raises():
    with pytest.raises(InputValidationError):
        validate_transition(JobState.BLOCKED, JobState.APPROVED)


def test_supersede_from_approved():
    validate_transition(JobState.APPROVED, JobState.SUPERSEDED)
    validate_transition(JobState.REVIEW_PENDING, JobState.SUPERSEDED)
    validate_transition(JobState.NEEDS_REVISION, JobState.SUPERSEDED)


def test_needs_human_review_can_supersede_and_resolve():
    # NHR is no longer a dead-end: it can exit to PROCESSED (resolve), REJECTED
    # (reject), or SUPERSEDED (redo).
    validate_transition(JobState.NEEDS_HUMAN_REVIEW, JobState.PROCESSED)
    validate_transition(JobState.NEEDS_HUMAN_REVIEW, JobState.REJECTED)
    validate_transition(JobState.NEEDS_HUMAN_REVIEW, JobState.SUPERSEDED)


def test_retry_edges():
    validate_transition(JobState.CRAWL_FAILED, JobState.NEW)
    validate_transition(JobState.PROCESS_FAILED, JobState.PROCESSING)
    validate_transition(JobState.CRAWLED_WARN, JobState.PROCESSING)


def test_no_review_pending_to_processing_edge():
    # Freeze is enforced by EDGE ABSENCE, not a guard.
    assert not is_legal_transition(JobState.REVIEW_PENDING, JobState.PROCESSING)
    assert JobState.PROCESSING not in allowed_transitions(JobState.REVIEW_PENDING)
    with pytest.raises(InputValidationError):
        validate_transition(JobState.REVIEW_PENDING, JobState.PROCESSING)


def test_every_non_terminal_state_has_exit_edge():
    for st in JobState:
        if st in TERMINAL_STATES:
            assert allowed_transitions(st) == frozenset()
        else:
            assert allowed_transitions(st), f"{st} has no exit edge"


def test_terminal_states_have_no_exits():
    for st in TERMINAL_STATES:
        assert allowed_transitions(st) == frozenset()
    # SUPERSEDED stays terminal: recovery never reopens a job in place (U8).
    assert JobState.SUPERSEDED in TERMINAL_STATES


def test_blocked_duplicate_are_operator_recoverable_to_superseded():
    # U8: BLOCKED/DUPLICATE are no longer terminal — they carry a single
    # operator-only recovery edge to SUPERSEDED.
    assert JobState.BLOCKED not in TERMINAL_STATES
    assert JobState.DUPLICATE not in TERMINAL_STATES
    assert is_legal_transition(JobState.BLOCKED, JobState.SUPERSEDED)
    assert is_legal_transition(JobState.DUPLICATE, JobState.SUPERSEDED)


def test_recovery_edge_is_not_a_laundering_path():
    # Anti-laundering (U8): the EXACT successor set of BLOCKED/DUPLICATE must be
    # {SUPERSEDED} — no edge to PROCESSING/CRAWLED or anywhere else — and
    # SUPERSEDED must stay a dead-end {}. A future stray "reopen" edge to ANY
    # other state fails this, which is the whole point (it would turn the
    # recovery edge into a content-laundering bypass of the gate chain).
    assert allowed_transitions(JobState.BLOCKED) == frozenset({JobState.SUPERSEDED})
    assert allowed_transitions(JobState.DUPLICATE) == frozenset({JobState.SUPERSEDED})
    assert allowed_transitions(JobState.SUPERSEDED) == frozenset()
    # The only way back into review is a brand-new job re-entering at NEW.
    assert not is_legal_transition(JobState.BLOCKED, JobState.PROCESSING)
    assert not is_legal_transition(JobState.BLOCKED, JobState.CRAWLED)
    assert not is_legal_transition(JobState.DUPLICATE, JobState.PROCESSING)
    assert not is_legal_transition(JobState.DUPLICATE, JobState.CRAWLED)


def test_processing_is_transient():
    assert JobState.PROCESSING in TRANSIENT_STATES


def test_side_branches_have_exits():
    # NEEDS_* must exit; BLOCKED/DUPLICATE now exit only via the operator
    # recovery edge to SUPERSEDED (asserted in the U8 tests above).
    assert allowed_transitions(JobState.NEEDS_HUMAN_REVIEW)
    assert allowed_transitions(JobState.NEEDS_REVISION)
    # crawl/process failures retry back into the pipeline
    assert JobState.NEW in allowed_transitions(JobState.CRAWL_FAILED)
    assert JobState.PROCESSING in allowed_transitions(JobState.PROCESS_FAILED)


def test_needs_human_review_reasons_exist():
    assert {r.value for r in ReviewReason} == {"risk", "dedup", "grounding"}
