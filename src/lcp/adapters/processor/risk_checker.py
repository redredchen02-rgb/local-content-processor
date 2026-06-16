"""Risk gate orchestration (imperative shell).

Loads risk inputs, calls the pure :mod:`lcp.core.rules.risk_rules`, maps the
:class:`RiskResult` onto a :class:`~lcp.core.state.JobState`, and writes audit:
  * blocked (redline)  -> BLOCKED (terminal, not overridable by default)
  * needs_human_review -> NEEDS_HUMAN_REVIEW + ReviewReason.RISK
  * pass               -> caller continues (no state write here)

The pure layer makes ALL the judgement; this file only wires inputs/outputs and
records a PII-free audit event (category codes + flag count, never raw text)."""

from __future__ import annotations

from dataclasses import dataclass

from ...core.rules import risk_rules
from ...core.rules.risk_rules import RiskInput, RiskResult, RiskStatus
from ...core.state import JobState, ReviewReason
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from ._persist import persist_gate_state

# Audit event vocabulary for this gate.
EVENT_RISK_GATE = "RISK_GATE"


@dataclass(frozen=True)
class RiskGateOutcome:
    """What the gate did: the pure result + the persisted state (if any)."""

    result: RiskResult
    job_state: JobState | None  # None when status==pass (caller continues)
    review_reason: ReviewReason | None = None


def _map_to_state(result: RiskResult) -> tuple[JobState | None, ReviewReason | None]:
    if result.status == RiskStatus.BLOCKED:
        return JobState.BLOCKED, None
    if result.status == RiskStatus.NEEDS_HUMAN_REVIEW:
        return JobState.NEEDS_HUMAN_REVIEW, ReviewReason.RISK
    return None, None  # PASS -> caller continues


def run_risk_gate(
    *,
    job_id: str,
    content: RiskInput,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    actor: str = "system",
    detector: risk_rules.RiskDetector | None = None,
    enabled_categories: frozenset[risk_rules.RiskCategory] | None = None,
) -> RiskGateOutcome:
    """Run the risk gate for a job and persist the resulting state.

    `ts` is supplied by the caller (deterministic, like the rest of the codebase).
    Returns the outcome so the pipeline can decide whether to continue."""
    result = risk_rules.assess_risk(
        content, detector, enabled_categories=enabled_categories
    )
    job_state, review_reason = _map_to_state(result)

    # PII-free audit: category codes + counts only.
    audit.append(
        ts=ts,
        stage="risk",
        event=EVENT_RISK_GATE,
        job_id=job_id,
        actor=actor,
        extra={
            "status": result.status.value,
            "flag_categories": sorted({f.category.value for f in result.flags}),
            "flag_count": len(result.flags),
            "review_reason": review_reason.value if review_reason else None,
            "recommended_action": result.recommended_action,
        },
    )

    if job_state is not None:
        persist_gate_state(
            store,
            job_id,
            job_state,
            updated_at=ts,
            review_reason=review_reason,
        )
    return RiskGateOutcome(
        result=result, job_state=job_state, review_reason=review_reason
    )
