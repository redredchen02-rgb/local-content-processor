"""Draft lint + grounding gate orchestration (imperative shell).

Runs the pure :mod:`lcp.core.rules.lint_rules` and :mod:`lcp.core.rules.grounding`
over a :class:`~lcp.core.draft.Draft`, writes a PII-free audit event, and maps
the outcome onto a :class:`~lcp.core.state.JobState` via the shared
:func:`persist_gate_state` (consistent with the risk / dedup gates):

  * grounding fail  -> NEEDS_HUMAN_REVIEW + ReviewReason.GROUNDING  (highest
    precedence — an ungrounded claim is the most serious Stage-2 lint outcome)
  * lint needs_revision / blocked -> NEEDS_REVISION
  * both pass        -> caller continues toward PROCESSED (no state write here)

After a human clears the grounding hold, lint must RE-RUN (plan 架構審查 2d):
:func:`relint_after_grounding_cleared` is that path — it runs lint only (the
human already vouched for grounding) and persists NEEDS_REVISION if lint still
fails, or returns a clean outcome the caller can drive to PROCESSED.

REDLINE 3 (R41): the pure rules this shell calls do ONLY local string
comparison — they never parse/resolve/fetch a URL. This shell does no URL I/O
either; the source is cleaned with the assembler's ``sanitize_source`` (the same
cleaning grounding uses) and compared locally. The audit stays PII-free: it
records statuses, counts, and the review_reason CODE — never titles/bodies/URLs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.config import ContentConfig
from ...core.draft import Draft
from ...core.rules import grounding as grounding_rules
from ...core.rules import lint_rules
from ...core.rules.grounding import GroundingResult, GroundingStrategy
from ...core.rules.lint_rules import LintConfig, LintResult, LintStatus
from ...core.state import JobState, ReviewReason
from ...core.text_sanitize import sanitize_source
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from ._persist import persist_gate_state

EVENT_LINT_GATE = "LINT_GATE"
EVENT_GROUNDING_GATE = "GROUNDING_GATE"


@dataclass(frozen=True)
class DraftLintOutcome:
    """What the gate did: the two pure results + the persisted state (if any)."""

    lint: LintResult | None  # None when grounding failed first (lint not run)
    grounding: GroundingResult | None  # None on the re-lint path
    job_state: JobState | None  # None when both pass (caller continues)
    review_reason: ReviewReason | None = None


def build_lint_config(content: ContentConfig, categories: dict[str, list[str]]) -> LintConfig:
    """Project the loaded :class:`ContentConfig` (+ the config categories dict)
    into the pure :class:`LintConfig` value object the rule module consumes."""
    return LintConfig(
        title_min_chars=content.title_min_chars,
        title_max_chars=content.title_max_chars,
        tag_min_count=content.tag_min_count,
        tag_max_count=content.tag_max_count,
        categories=tuple(categories.keys()),
    )


def _source_paragraphs(source_text: str) -> list[str]:
    """Clean the source IDENTICALLY to grounding, then split into paragraphs for
    the copied-too-much check. Local string ops only — no URL parsing."""
    cleaned = sanitize_source(source_text or "")
    return lint_rules._paragraphs(cleaned)


def run_draft_lint_gate(
    *,
    job_id: str,
    draft: Draft,
    source_text: str,
    lint_config: LintConfig,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    has_videos: bool = False,
    has_images: bool = False,
    grounding_strategy: GroundingStrategy | None = None,
    actor: str = "system",
) -> DraftLintOutcome:
    """Run grounding + lint for a job and persist the resulting state.

    Order: grounding FIRST (a faithfulness failure is the most serious outcome
    and routes to a human). If grounding passes, lint runs; lint failures route
    to NEEDS_REVISION. `ts` is supplied by the caller (deterministic). Returns
    the outcome so the pipeline can decide whether to continue toward PROCESSED.

    Pure judgement is entirely in the rule modules; this only wires I/O + maps to
    state, exactly like the risk / dedup gates."""
    # --- grounding (verbatim quotes + claims; local-only, no URL parse) ----
    grounding_result = grounding_rules.verify_grounding(
        draft, source_text, grounding_strategy
    )
    audit.append(
        ts=ts,
        stage="grounding",
        event=EVENT_GROUNDING_GATE,
        job_id=job_id,
        actor=actor,
        extra={
            "status": grounding_result.status.value,
            "ungrounded_quote_count": sum(
                1 for u in grounding_result.ungrounded_claims if u.kind == "quote"
            ),
            "ungrounded_claim_count": sum(
                1 for u in grounding_result.ungrounded_claims if u.kind == "claim"
            ),
            "review_reason": (
                ReviewReason.GROUNDING.value
                if grounding_result.needs_human_review
                else None
            ),
        },
    )

    if grounding_result.needs_human_review:
        persist_gate_state(
            store,
            job_id,
            JobState.NEEDS_HUMAN_REVIEW,
            updated_at=ts,
            review_reason=ReviewReason.GROUNDING,
        )
        return DraftLintOutcome(
            lint=None,
            grounding=grounding_result,
            job_state=JobState.NEEDS_HUMAN_REVIEW,
            review_reason=ReviewReason.GROUNDING,
        )

    # --- lint (structure / quality; local-only, no URL parse) --------------
    lint_result = _run_lint(
        draft, lint_config, source_text, has_videos, job_id, audit, ts, actor,
        has_images=has_images,
    )

    if lint_result.status in (LintStatus.NEEDS_REVISION, LintStatus.BLOCKED):
        persist_gate_state(
            store, job_id, JobState.NEEDS_REVISION, updated_at=ts
        )
        return DraftLintOutcome(
            lint=lint_result,
            grounding=grounding_result,
            job_state=JobState.NEEDS_REVISION,
        )

    return DraftLintOutcome(
        lint=lint_result, grounding=grounding_result, job_state=None
    )


def relint_after_grounding_cleared(
    *,
    job_id: str,
    draft: Draft,
    source_text: str,
    lint_config: LintConfig,
    audit: AuditLog,
    ts: str,
    has_videos: bool = False,
    has_images: bool = False,
    actor: str = "human",
) -> DraftLintOutcome:
    """Re-run LINT after a human cleared the grounding hold (plan 架構審查 2d).

    Grounding is NOT re-evaluated here — the human already vouched for it when
    they cleared NEEDS_HUMAN_REVIEW(reason=grounding). We only re-lint structure/
    quality and write an audit event; we do NOT persist a state change. The legal
    transition out of NEEDS_HUMAN_REVIEW is the human's to make (the only edges
    are -> PROCESSED / -> REJECTED), so this gate just hands back the fresh lint
    result for the human-clear path to act on:
      * lint passes -> the caller commits NEEDS_HUMAN_REVIEW -> PROCESSED.
      * lint fails  -> the caller keeps the job for the human (re-edit /
        supersede); we do NOT silently push an illegal transition.

    Returns ``job_state=None`` always (no persist here); inspect
    ``outcome.lint`` to decide the next move."""
    lint_result = _run_lint(
        draft, lint_config, source_text, has_videos, job_id, audit, ts, actor,
        has_images=has_images,
    )
    return DraftLintOutcome(lint=lint_result, grounding=None, job_state=None)


def relint_clears_hold(
    *,
    job_id: str,
    draft: Draft,
    source_text: str,
    lint_config: LintConfig,
    audit: AuditLog,
    ts: str,
    has_videos: bool = False,
    has_images: bool = False,
    actor: str = "human",
) -> bool:
    """Re-run lint for the grounding-cleared resolve path and report whether it
    CLEARS the hold (a clean lint PASS).

    Owns the lint PASS/refuse *interpretation* so the publisher (signoff.resolve)
    no longer imports LintStatus — it consumes only this boolean and keeps the
    state transition + the operator-facing refusal. Emits the SAME single
    LINT_GATE audit event (actor=<the resolving reviewer>) as
    relint_after_grounding_cleared (this delegates to it)."""
    outcome = relint_after_grounding_cleared(
        job_id=job_id,
        draft=draft,
        source_text=source_text,
        lint_config=lint_config,
        audit=audit,
        ts=ts,
        has_videos=has_videos,
        has_images=has_images,
        actor=actor,
    )
    return outcome.lint is not None and outcome.lint.passed


def _run_lint(
    draft: Draft,
    lint_config: LintConfig,
    source_text: str,
    has_videos: bool,
    job_id: str,
    audit: AuditLog,
    ts: str,
    actor: str,
    has_images: bool = False,
) -> LintResult:
    """Run pure lint + write a PII-free audit event. Helper for both paths."""
    lint_result = lint_rules.lint_draft(
        draft,
        lint_config,
        source_paragraphs=_source_paragraphs(source_text),
        has_videos=has_videos,
        has_images=has_images,
    )
    audit.append(
        ts=ts,
        stage="lint",
        event=EVENT_LINT_GATE,
        job_id=job_id,
        actor=actor,
        extra={
            "status": lint_result.status.value,
            "error_count": len(lint_result.errors),
            "warning_count": len(lint_result.warnings),
            "score": lint_result.score,
        },
    )
    return lint_result
