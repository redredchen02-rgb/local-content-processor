"""Sign-off + responsibility loop (Unit 8).

WHAT sign-off IS — and is NOT:
  * It is ATTRIBUTION, not AUTHENTICATION. On a single-user local machine there
    is no real auth; we record who CLAIMED to review (`reviewer_stated`, picked
    from a config whitelist) AND the OBSERVED OS user (`pwd.getpwuid(os.getuid())
    .pw_name` — harder to forge than the getpass/env-trusting alternative). Both
    are stamped into the append-only audit, alongside a VERBATIM disclaimer
    (:data:`DISCLAIMER`) so the limitation is on the record, not implied.

  * The machine NEVER publishes (plan R26). `approve` moves REVIEW_PENDING ->
    APPROVED only. PUBLISHED_RECORDED is reached ONLY after a human pastes the
    real published URL and ticks the attestation checkbox (backfill). Until then
    the job is NOT complete (plan R37) — it shows up in the APPROVED worklist.

HASH BINDING (the integrity core): a sign-off binds to the FROZEN draft body +
title + cover hashes recorded by the review packet. We re-read the freeze record
and re-verify the caller's draft still hashes to the same body/title before
approving — so editing the BODY after the packet was built is detectable (hash
mismatch -> refusal). The bound hashes are written into the audit event so the
sign-off provably covers that exact artifact.

SUPERSEDE: redoing an already-signed (or pending) job is a first-class terminal
operation. `supersede` moves a supersede-able state -> SUPERSEDED, voids the old
sign-off (audit SIGNOFF_INVALIDATED + SUPERSEDED) when there was one, and
back-links the new job id. It is also the operator RECOVERY seam (U8) for a
false-terminal job: a DUPLICATE recovers via the ordinary single-step path, while
a BLOCKED (redline) recovery requires an explicit second confirmation
(``redline_override=True``) and records a DISTINCT event (REDLINE_OVERRIDE,
carrying the original blocking RiskCategory codes), never reusing the abandon
path. SUPERSEDED stays terminal — recovery never reopens the job in place; the
only way back into review is a brand-new job re-entering at NEW.

PII: the audit stays PII-free — reviewer/OS-user NAMES are operator identifiers
(not subject PII) and the disclaimer/url-recorded flag carry no scraped text.
Artifact CONTENT hashes are high-entropy and allowed."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ...core.config import Config
from ...core.draft import Draft
from ...core.errors import InputValidationError
from ...core.state import JobState, ReviewReason
from ..processor.risk_checker import EVENT_RISK_GATE
from ..storage.audit_log import (
    EVENT_REDLINE_OVERRIDE,
    EVENT_SIGNOFF_INVALIDATED,
    EVENT_SUPERSEDED,
    AuditLog,
)
from ..storage.job_store import JobStore
from .review_packet import (
    compute_body_sha256,
    compute_review_cover_sha256,
    compute_title_sha256,
    read_review_manifest,
)

EVENT_SIGNOFF_APPROVE = "SIGNOFF_APPROVE"
EVENT_SIGNOFF_REJECT = "SIGNOFF_REJECT"
EVENT_PUBLISHED_RECORDED = "PUBLISHED_RECORDED"
EVENT_NHR_RESOLVED = "NHR_RESOLVED"

# VERBATIM disclaimer — recorded with every sign-off. Do not paraphrase: it is
# the honest statement that this is attribution, not authentication (plan).
DISCLAIMER = (
    "ATTRIBUTION, NOT AUTHENTICATION: this sign-off records who STATED they "
    "reviewed this content on a single-user local machine. It is NOT identity "
    "verification and provides no cryptographic proof of who acted. The stated "
    "reviewer and the observed OS user are recorded for accountability only."
)

# States from which a job may be superseded (redo). APPROVED is included on
# purpose: an already-signed job can be re-done, voiding the old sign-off (plan).
# NEEDS_HUMAN_REVIEW is included so a held job can be re-done instead of being
# stuck (the state machine carries the matching edge).
#
# BLOCKED / DUPLICATE were added (U8) so an operator can RECOVER a false-terminal
# job. The state-table edge alone is NOT enough — `supersede` independently
# refuses any source state not in this frozenset, so both must be widened in
# lockstep. (Regression guard test: dropping these two from the set must make a
# BLOCKED supersede refuse again, even with the state edge present.)
_SUPERSEDABLE = frozenset(
    {
        JobState.REVIEW_PENDING,
        JobState.APPROVED,
        JobState.NEEDS_REVISION,
        JobState.NEEDS_HUMAN_REVIEW,
        JobState.BLOCKED,
        JobState.DUPLICATE,
    }
)

# Source states that were NEVER signed off. Superseding any of these must NOT
# emit SIGNOFF_INVALIDATED — "void the old sign-off" would be a FALSE audit
# statement, undermining the audit chain's truthfulness (U8). A real sign-off is
# only produced by `approve` at REVIEW_PENDING (-> APPROVED); every other
# supersede-able source reaches its state BEFORE the freeze/review-packet step:
# BLOCKED/DUPLICATE (terminal-recovery, U8) and the held NEEDS_REVISION /
# NEEDS_HUMAN_REVIEW (Stage-2 gate holds, pre-freeze) were all never signed off.
# So SIGNOFF_INVALIDATED is emitted ONLY for the complement: {REVIEW_PENDING,
# APPROVED}. (bug_008: NEEDS_* were previously omitted from this set, so a routine
# abandon of a held job wrote a false invalidation event.)
_NEVER_SIGNED_OFF = frozenset(
    {
        JobState.BLOCKED,
        JobState.DUPLICATE,
        JobState.NEEDS_REVISION,
        JobState.NEEDS_HUMAN_REVIEW,
    }
)


@dataclass(frozen=True)
class SignoffRecord:
    """The persisted sign-off outcome (also what the GUI/CLI echoes back)."""

    job_id: str
    decision: str  # "approved" | "rejected"
    reviewer_stated: str
    observed_os_user: str
    body_sha256: str
    title_sha256: str
    cover_sha256: str | None
    new_state: JobState
    disclaimer: str = DISCLAIMER


def observed_os_user() -> str:
    """The OS user as observed from the kernel, not from a trust-the-env source.

    Prefers ``pwd.getpwuid(os.getuid()).pw_name`` (harder to forge than
    ``getpass.getuser()``, which trusts $USER/$LOGNAME). Falls back gracefully
    on platforms without ``pwd`` (e.g. Windows) or odd uid mappings."""
    try:
        import pwd  # POSIX-only

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        try:
            import getpass

            return getpass.getuser()
        except Exception:
            return "unknown"


def _require_whitelisted(config: Config, reviewer: str) -> None:
    if not config.publisher.reviewers:
        return
    if reviewer not in config.publisher.reviewers:
        raise InputValidationError(
            f"reviewer not in whitelist: {reviewer!r} "
            f"(configured reviewers: {sorted(config.publisher.reviewers)!r})"
        )


def _freeze_hashes(store: JobStore, job_id: str) -> dict[str, Any]:
    manifest = read_review_manifest(store, job_id)
    if manifest is None or "freeze" not in manifest:
        raise InputValidationError(
            f"no review packet freeze record for job {job_id}; build the review "
            "packet first"
        )
    freeze: dict[str, Any] = manifest["freeze"]
    return freeze


def approve(
    job_id: str,
    reviewer: str,
    *,
    config: Config,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    draft: Draft | None = None,
) -> SignoffRecord:
    """Approve a REVIEW_PENDING job: REVIEW_PENDING -> APPROVED.

    Refuses (InputValidationError, audited) if the reviewer is not whitelisted.
    Refuses (the state machine raises) for any non-REVIEW_PENDING source state —
    so BLOCKED / DUPLICATE / NEEDS_HUMAN_REVIEW have NO path to APPROVED.

    The frozen body binding is ALWAYS enforced: if `draft` is None we load the
    persisted Stage-2 draft ourselves (belt-and-suspenders so a shell that
    forgets to pass it still gets the check). We re-verify the draft still hashes
    to the FROZEN body recorded by the review packet; a mismatch means the body
    was edited after freeze and approval is refused. The bound hashes are written
    into the audit event so the sign-off provably covers that artifact. Does NOT
    publish (R26) — APPROVED is the most a machine action ever reaches."""
    observed = observed_os_user()
    try:
        _require_whitelisted(config, reviewer)
    except InputValidationError:
        audit.append(
            ts=ts,
            stage="signoff",
            event=EVENT_SIGNOFF_REJECT,
            job_id=job_id,
            actor=observed,
            extra={
                "reviewer_stated": reviewer,
                "observed_os_user": observed,
                "reason": "reviewer_not_whitelisted",
                "disclaimer": DISCLAIMER,
            },
        )
        raise

    freeze = _freeze_hashes(store, job_id)
    body_sha = freeze.get("body_sha256")
    title_sha = freeze.get("title_sha256")
    cover_sha = freeze.get("cover_sha256")

    # Fail closed on a malformed freeze: an approval must bind to a real
    # body+title hash, never a null one. (Also narrows Optional -> str for the
    # SignoffRecord construction below.)
    if not isinstance(body_sha, str) or not isinstance(title_sha, str):
        raise InputValidationError(
            f"freeze record for {job_id} is missing the bound body/title hash; "
            "rebuild the review packet before approving"
        )

    # Belt-and-suspenders: if the shell did not pass the draft, load the
    # persisted one ourselves so the body binding is enforced unconditionally.
    if draft is None:
        from ..storage.draft_store import load_draft

        draft = load_draft(store, job_id)

    # Fail loud if the draft is not available — the hash binding MUST run;
    # silently skipping it (the old `if draft is not None:` guard) would allow
    # approving an artifact whose body we cannot verify.
    if draft is None:
        raise InputValidationError(
            f"draft not found for {job_id}: cannot verify body hash binding; "
            "re-run Stage-2 to regenerate the draft before approving"
        )

    # Hash binding: the body AND title MUST match the frozen hashes. Editing
    # either after the packet was built is detectable here and blocks approval —
    # the freeze covers body + title (+ cover below), so the sign-off provably
    # binds the artifact the reviewer actually saw. (U3: previously only the body
    # was re-verified; a title-only edit slipped through while the audit still
    # attested the original title.)
    current_body = compute_body_sha256(draft)
    if current_body != body_sha:
        raise InputValidationError(
            f"draft body hash mismatch for {job_id}: the body changed after "
            "the review packet was frozen; supersede and re-review instead"
        )
    current_title = compute_title_sha256(draft)
    if current_title != title_sha:
        raise InputValidationError(
            f"draft title hash mismatch for {job_id}: the title changed after "
            "the review packet was frozen; supersede and re-review instead"
        )

    # Cover binding (file-based, independent of `draft`): if the freeze bound a
    # cover, re-hash the review-dir cover.jpg it was copied from and refuse on a
    # mismatch (a post-freeze cover swap).
    if isinstance(cover_sha, str):
        current_cover = compute_review_cover_sha256(store, job_id)
        if current_cover != cover_sha:
            raise InputValidationError(
                f"cover hash mismatch for {job_id}: the review cover changed after "
                "the review packet was frozen; supersede and re-review instead"
            )

    # State transition (raises if illegal — the only legal source is
    # REVIEW_PENDING, so BLOCKED/DUPLICATE/NEEDS_HUMAN_REVIEW are rejected here).
    store.set_state(job_id, JobState.APPROVED, updated_at=ts)

    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_SIGNOFF_APPROVE,
        job_id=job_id,
        actor=observed,
        artifact_sha256=body_sha,
        extra={
            "reviewer_stated": reviewer,
            "observed_os_user": observed,
            "bound_title_sha256": title_sha,
            "bound_cover_sha256": cover_sha,
            "disclaimer": DISCLAIMER,
        },
    )

    return SignoffRecord(
        job_id=job_id,
        decision="approved",
        reviewer_stated=reviewer,
        observed_os_user=observed,
        body_sha256=body_sha,
        title_sha256=title_sha,
        cover_sha256=cover_sha,
        new_state=JobState.APPROVED,
    )


def reject(
    job_id: str,
    reviewer: str,
    reason: str,
    *,
    config: Config,
    store: JobStore,
    audit: AuditLog,
    ts: str,
) -> SignoffRecord:
    """Reject a REVIEW_PENDING / APPROVED / NEEDS_HUMAN_REVIEW job -> REJECTED.

    Whitelist + state-machine enforced like `approve`. A FREEZE record is only
    required when rejecting from REVIEW_PENDING / APPROVED (those jobs always have
    a review packet, and we bind the rejected artifact's body hash). A
    NEEDS_HUMAN_REVIEW job (risk/dedup/grounding hold) has NO packet, so it would
    otherwise be stuck — rejecting it must NOT require a freeze; the state machine
    (NEEDS_HUMAN_REVIEW -> REJECTED) is the gate. `reason` is an operator note
    recorded under a PII-safe key (`reject_note`); keep it free of scraped text —
    it is the reviewer's own words, not subject PII."""
    observed = observed_os_user()
    try:
        _require_whitelisted(config, reviewer)
    except InputValidationError:
        audit.append(
            ts=ts,
            stage="signoff",
            event=EVENT_SIGNOFF_REJECT,
            job_id=job_id,
            actor=observed,
            extra={
                "reviewer_stated": reviewer,
                "observed_os_user": observed,
                "reason": "reviewer_not_whitelisted",
                "disclaimer": DISCLAIMER,
            },
        )
        raise

    record = store.get_job(job_id)
    if record is None:
        raise InputValidationError(f"unknown job: {job_id}")

    # Freeze is only meaningful for packet-bearing states. A NEEDS_HUMAN_REVIEW
    # job has no packet — requiring a freeze would dead-end it (the original bug).
    freeze: dict[str, Any] = {}
    if record.state is not JobState.NEEDS_HUMAN_REVIEW:
        freeze = _freeze_hashes(store, job_id)

    store.set_state(job_id, JobState.REJECTED, updated_at=ts)

    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_SIGNOFF_REJECT,
        job_id=job_id,
        actor=observed,
        artifact_sha256=freeze.get("body_sha256"),
        extra={
            "reviewer_stated": reviewer,
            "observed_os_user": observed,
            "reject_note": reason,
            "rejected_from_state": record.state.value,
            "disclaimer": DISCLAIMER,
        },
    )

    return SignoffRecord(
        job_id=job_id,
        decision="rejected",
        reviewer_stated=reviewer,
        observed_os_user=observed,
        # A packet-less rejection (e.g. NEEDS_HUMAN_REVIEW) has no frozen hashes;
        # record the absence as "" (the codebase's no-hash convention).
        body_sha256=freeze.get("body_sha256") or "",
        title_sha256=freeze.get("title_sha256") or "",
        cover_sha256=freeze.get("cover_sha256"),
        new_state=JobState.REJECTED,
    )


def resolve(
    job_id: str,
    reviewer: str,
    *,
    config: Config,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    relint: bool = False,
    reason: str | None = None,
) -> SignoffRecord:
    """Operator path OUT of NEEDS_HUMAN_REVIEW: NEEDS_HUMAN_REVIEW -> PROCESSED.

    A held job (risk / dedup / grounding) otherwise has no command that drives it
    forward — this is that command. It is honest about WHY each hold clears:

      * GROUNDING hold + ``relint=True``: re-run lint (the human already vouched
        for grounding when they cleared it, plan 架構審查 2d). Only a CLEAN lint
        promotes the job to PROCESSED; a still-failing lint refuses (keep it for
        the human to re-edit / supersede).
      * RISK / DEDUP hold (or a grounding hold without relint): a human OVERRIDE.
        The state machine already allows NHR -> PROCESSED; we require an explicit
        ``reason`` and record it (reviewer + reason) so the override is on the
        audit record, not silent.

    Whitelist-enforced like approve/reject. The job MUST currently be in
    NEEDS_HUMAN_REVIEW. Returns a SignoffRecord (decision="resolved")."""
    observed = observed_os_user()
    _require_whitelisted(config, reviewer)

    record = store.get_job(job_id)
    if record is None:
        raise InputValidationError(f"unknown job: {job_id}")
    if record.state is not JobState.NEEDS_HUMAN_REVIEW:
        raise InputValidationError(
            f"resolve requires a NEEDS_HUMAN_REVIEW job; {job_id} is "
            f"{record.state.value}"
        )

    hold = record.review_reason
    mode: str
    if relint and hold is ReviewReason.GROUNDING:
        # Re-lint path: the human cleared grounding; lint must re-run clean.
        from ..storage.draft_store import _read_source_text, load_draft
        from ..processor.draft_linter import build_lint_config, relint_clears_hold
        from ..processor.media_checker import media_presence

        draft = load_draft(store, job_id)
        if draft is None:
            raise InputValidationError(
                f"no processed draft for {job_id}; cannot re-lint to resolve"
            )
        lint_config = build_lint_config(config.content, config.categories)
        # Re-lint must apply the SAME media-conditional section rules the first
        # lint would have (D9): image_sections is required IFF the bundle has
        # images. Recover (has_images, has_videos) from the persisted media
        # report — without this the relint defaults both False and silently
        # never requires image_sections for an image-bearing job (fail-open).
        has_images, has_videos = media_presence(store, job_id)
        # The processor owns the lint PASS/refuse verdict (returns a bool);
        # signoff keeps the operator-facing refusal + the state transition. The
        # single LINT_GATE audit event (actor=reviewer) is emitted inside.
        cleared = relint_clears_hold(
            job_id=job_id,
            draft=draft,
            source_text=_read_source_text(store, job_id),
            lint_config=lint_config,
            audit=audit,
            ts=ts,
            has_videos=has_videos,
            has_images=has_images,
            actor=reviewer,
        )
        if not cleared:
            raise InputValidationError(
                f"re-lint still fails for {job_id}; the grounding hold cannot be "
                "auto-resolved — re-edit or supersede instead"
            )
        mode = "relint_clean"
    else:
        # Override path (risk / dedup, or grounding without relint): explicit +
        # audited. An override without a reason is refused — keep it honest.
        if not reason or not reason.strip():
            raise InputValidationError(
                "resolving a risk/dedup hold (or a grounding hold without "
                "--relint) is a human OVERRIDE and requires an explicit reason"
            )
        mode = "human_override"

    # State transition: NEEDS_HUMAN_REVIEW -> PROCESSED (state machine gate).
    store.set_state(job_id, JobState.PROCESSED, updated_at=ts)

    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_NHR_RESOLVED,
        job_id=job_id,
        actor=observed,
        extra={
            "reviewer_stated": reviewer,
            "observed_os_user": observed,
            "resolved_from_reason": hold.value if hold else None,
            "mode": mode,
            "override_note": reason if mode == "human_override" else None,
            "disclaimer": DISCLAIMER,
        },
    )

    return SignoffRecord(
        job_id=job_id,
        decision="resolved",
        reviewer_stated=reviewer,
        observed_os_user=observed,
        body_sha256="",
        title_sha256="",
        cover_sha256=None,
        new_state=JobState.PROCESSED,
    )


def backfill_published_url(
    job_id: str,
    url: str,
    *,
    config: Config,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    attested: bool,
    reviewer: str,
) -> JobState:
    """Close the responsibility loop: APPROVED -> PUBLISHED_RECORDED (plan R37).

    Requires a WHITELISTED reviewer (like approve/reject — recording a publish is
    an accountable operator action) PLUS BOTH a non-empty published URL AND an
    operator attestation tick
    (`attested=True`) confirming the published version IS the signed-off version.
    Without the tick (or with an empty URL) the job STAYS APPROVED — the machine
    does not publish and cannot complete the loop on its own (R26/R37).

    The URL is NOT stored in the PII-free SQLite index or audit text — only the
    fact that a URL was recorded + the body hash. The URL itself is written to a
    0600 file in the job dir (operator-facing, plaintext, best-effort deletion).
    We never fetch or resolve it."""
    # Recording a publish is an accountable action -> require a whitelisted
    # reviewer, exactly like approve/reject.
    _require_whitelisted(config, reviewer)

    record = store.get_job(job_id)
    if record is None:
        raise InputValidationError(f"unknown job: {job_id}")
    if record.state is not JobState.APPROVED:
        raise InputValidationError(
            f"backfill requires an APPROVED job; {job_id} is {record.state.value}"
        )
    if not url or not url.strip():
        raise InputValidationError("published URL is required to record publish")
    if not attested:
        # No tick -> stay APPROVED (not complete). Honest: the loop is open.
        raise InputValidationError(
            "operator attestation required: confirm the published version is the "
            "signed-off version (tick the attestation) — job stays APPROVED"
        )

    freeze = _freeze_hashes(store, job_id)

    # Record the URL to a 0600 operator file (NOT in SQLite/audit text).
    # Atomic write (temp + os.replace) so a crash mid-write never leaves a
    # partial URL in the destination file.
    review_dir = store.job_dir(job_id) / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    url_path = review_dir / "published_url.txt"
    tmp_path = url_path.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(url.strip() + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, url_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    store.set_state(job_id, JobState.PUBLISHED_RECORDED, updated_at=ts)

    # Capture once so both actor= and extra[observed_os_user] are identical
    # (two separate calls could race on a multi-user system).
    observed = observed_os_user()
    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_PUBLISHED_RECORDED,
        job_id=job_id,
        actor=observed,
        artifact_sha256=freeze.get("body_sha256"),
        extra={
            "reviewer_stated": reviewer,
            "observed_os_user": observed,
            "published_url_recorded": True,
            "operator_attested": True,
        },
    )
    return JobState.PUBLISHED_RECORDED


def _blocking_reason_codes(audit: AuditLog, job_id: str) -> list[str]:
    """Original redline RiskCategory CODES for a BLOCKED job, from the audit.

    The blocking reasons live transiently on ``RiskResult.flags`` and are NOT
    persisted in the jobs table, so we recover them from the prior RISK_GATE
    audit event (which records ``flag_categories`` — enum codes only, already
    PII-free). Returns CODES ONLY (never the free-text flag reason, which could
    carry a matched snippet), so the override audit stays PII-free. Degrades
    gracefully to ``[]`` if the event or its codes are not recoverable — the
    override is recorded either way; it never hard-fails on a missing source
    event."""
    codes: list[str] = []
    for line in audit.iter_events():
        if line.get("job_id") != job_id or line.get("event") != EVENT_RISK_GATE:
            continue
        cats = line.get("extra", {}).get("flag_categories")
        if isinstance(cats, list):
            codes = [c for c in cats if isinstance(c, str)]  # last gate wins
    return codes


def supersede(
    job_id: str,
    *,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    new_job_id: str | None = None,
    actor: str = "human",
    redline_override: bool = False,
) -> JobState:
    """Supersede a supersede-able job: -> SUPERSEDED (terminal).

    Two distinct paths share this seam:

    * ORDINARY ABANDON (REVIEW_PENDING / APPROVED / NEEDS_REVISION /
      NEEDS_HUMAN_REVIEW / DUPLICATE): a single-step operator action. Writes
      SUPERSEDED (with a back-link to the new job id, if given). SIGNOFF_INVALIDATED
      is emitted ONLY for source states that carried a real prior sign-off
      (REVIEW_PENDING / APPROVED) — a never-signed-off source (e.g. DUPLICATE)
      does NOT get it, since "void the old sign-off" would be a false statement.

    * REDLINE OVERRIDE (BLOCKED): recovering a terminal-redline job is a heavier,
      separately-confirmed action. It REQUIRES ``redline_override=True`` (the
      operator's explicit second confirmation — CLI ``--redline-override`` / a
      dedicated GUI dialog; a BLOCKED supersede without it is refused) and emits a
      DISTINCT event TYPE (EVENT_REDLINE_OVERRIDE, not EVENT_SUPERSEDED) carrying
      the original blocking RiskCategory codes. No SIGNOFF_INVALIDATED (a BLOCKED
      job was never signed off).

    ``actor`` should be the OBSERVED OS user (the shells pass
    ``observed_os_user()``); the ``"human"`` literal default is a last resort.
    The state machine is the real source-state gate — it raises for any state
    without a legal edge to SUPERSEDED. The new job itself is created by the
    caller/pipeline; this only records the supersession + link."""
    record = store.get_job(job_id)
    if record is None:
        raise InputValidationError(f"unknown job: {job_id}")
    if record.state not in _SUPERSEDABLE:
        raise InputValidationError(
            f"cannot supersede a {record.state.value} job ({job_id}); only "
            f"{sorted(s.value for s in _SUPERSEDABLE)} may be superseded"
        )

    is_redline = record.state is JobState.BLOCKED
    # A redline override is a deliberate, separately-confirmed action: refuse a
    # BLOCKED supersede that did not pass the second confirmation. (DUPLICATE is
    # not a redline state, so it takes the ordinary single-step path.)
    if is_redline and not redline_override:
        raise InputValidationError(
            f"recovering a BLOCKED (redline) job ({job_id}) requires an explicit "
            "redline override (a second confirmation); the ordinary abandon path "
            "may not be reused for a redline state"
        )

    # Void the old sign-off ONLY when there was one (never for a state that was
    # never signed off — emitting it would be a false audit statement, U8).
    if record.state not in _NEVER_SIGNED_OFF:
        audit.append(
            ts=ts,
            stage="signoff",
            event=EVENT_SIGNOFF_INVALIDATED,
            job_id=job_id,
            actor=actor,
            extra={"superseded_from_state": record.state.value},
        )

    store.set_state(job_id, JobState.SUPERSEDED, updated_at=ts)

    if is_redline:
        # Distinct event TYPE for the redline override, carrying the original
        # blocking RiskCategory codes (PII-free) so it is auditably distinct from
        # a routine abandon.
        audit.append(
            ts=ts,
            stage="signoff",
            event=EVENT_REDLINE_OVERRIDE,
            job_id=job_id,
            actor=actor,
            extra={
                "superseded_from_state": record.state.value,
                "blocking_reasons": _blocking_reason_codes(audit, job_id),
                "new_job_id": new_job_id,
            },
        )
        return JobState.SUPERSEDED

    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_SUPERSEDED,
        job_id=job_id,
        actor=actor,
        extra={"new_job_id": new_job_id},
    )
    return JobState.SUPERSEDED
