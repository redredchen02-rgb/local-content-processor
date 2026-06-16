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
operation. `supersede` moves a supersede-able state (REVIEW_PENDING / APPROVED /
NEEDS_REVISION) -> SUPERSEDED, voids the old sign-off (audit SIGNOFF_INVALIDATED
+ SUPERSEDED), and back-links the new job id.

PII: the audit stays PII-free — reviewer/OS-user NAMES are operator identifiers
(not subject PII) and the disclaimer/url-recorded flag carry no scraped text.
Artifact CONTENT hashes are high-entropy and allowed."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ...core.config import Config
from ...core.draft import Draft
from ...core.errors import InputValidationError
from ...core.state import JobState
from ..storage.audit_log import (
    EVENT_SIGNOFF_INVALIDATED,
    EVENT_SUPERSEDED,
    AuditLog,
)
from ..storage.job_store import JobStore
from .review_packet import compute_body_sha256, read_review_manifest

EVENT_SIGNOFF_APPROVE = "SIGNOFF_APPROVE"
EVENT_SIGNOFF_REJECT = "SIGNOFF_REJECT"
EVENT_PUBLISHED_RECORDED = "PUBLISHED_RECORDED"

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
_SUPERSEDABLE = frozenset(
    {JobState.REVIEW_PENDING, JobState.APPROVED, JobState.NEEDS_REVISION}
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
    if reviewer not in config.publisher.reviewers:
        raise InputValidationError(
            f"reviewer not in whitelist: {reviewer!r} "
            f"(configured reviewers: {sorted(config.publisher.reviewers)!r})"
        )


def _freeze_hashes(store: JobStore, job_id: str) -> dict:
    manifest = read_review_manifest(store, job_id)
    if manifest is None or "freeze" not in manifest:
        raise InputValidationError(
            f"no review packet freeze record for job {job_id}; build the review "
            "packet first"
        )
    return manifest["freeze"]


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

    If `draft` is supplied, re-verifies it still hashes to the FROZEN body +
    title recorded by the review packet; a mismatch means the body was edited
    after freeze and approval is refused. The bound hashes are written into the
    audit event so the sign-off provably covers that artifact. Does NOT publish
    (R26) — APPROVED is the most a machine action ever reaches."""
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

    # Hash binding: the body MUST match the frozen hash. Editing the body after
    # the packet was built is detectable here and blocks approval.
    if draft is not None:
        current_body = compute_body_sha256(draft)
        if current_body != body_sha:
            raise InputValidationError(
                f"draft body hash mismatch for {job_id}: the body changed after "
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
    """Reject a REVIEW_PENDING job: REVIEW_PENDING -> REJECTED (terminal).

    Whitelist + state-machine enforced exactly like `approve`. `reason` is an
    operator note recorded under a PII-safe key (`reject_note`); keep it free of
    scraped text — it is the reviewer's own words, not subject PII."""
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
            "disclaimer": DISCLAIMER,
        },
    )

    return SignoffRecord(
        job_id=job_id,
        decision="rejected",
        reviewer_stated=reviewer,
        observed_os_user=observed,
        body_sha256=freeze.get("body_sha256"),
        title_sha256=freeze.get("title_sha256"),
        cover_sha256=freeze.get("cover_sha256"),
        new_state=JobState.REJECTED,
    )


def backfill_published_url(
    job_id: str,
    url: str,
    *,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    attested: bool,
    reviewer: str | None = None,
) -> JobState:
    """Close the responsibility loop: APPROVED -> PUBLISHED_RECORDED (plan R37).

    Requires BOTH a non-empty published URL AND an operator attestation tick
    (`attested=True`) confirming the published version IS the signed-off version.
    Without the tick (or with an empty URL) the job STAYS APPROVED — the machine
    does not publish and cannot complete the loop on its own (R26/R37).

    The URL is NOT stored in the PII-free SQLite index or audit text — only the
    fact that a URL was recorded + the body hash. The URL itself is written to a
    0600 file in the job dir (operator-facing, plaintext, best-effort deletion).
    We never fetch or resolve it."""
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
    review_dir = store.job_dir(job_id) / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    url_path = review_dir / "published_url.txt"
    with url_path.open("w", encoding="utf-8") as f:
        f.write(url.strip() + "\n")
    try:
        os.chmod(url_path, 0o600)
    except OSError:
        pass

    store.set_state(job_id, JobState.PUBLISHED_RECORDED, updated_at=ts)

    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_PUBLISHED_RECORDED,
        job_id=job_id,
        actor=observed_os_user(),
        artifact_sha256=freeze.get("body_sha256"),
        extra={
            "reviewer_stated": reviewer,
            "observed_os_user": observed_os_user(),
            "published_url_recorded": True,
            "operator_attested": True,
        },
    )
    return JobState.PUBLISHED_RECORDED


def supersede(
    job_id: str,
    *,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    new_job_id: str | None = None,
    actor: str = "human",
) -> JobState:
    """Supersede a supersede-able job: -> SUPERSEDED (terminal), voiding any
    existing sign-off.

    Legal sources: REVIEW_PENDING / APPROVED / NEEDS_REVISION (the state machine
    is the real gate — it raises for anything else). Writes SIGNOFF_INVALIDATED
    (the old approval no longer stands) and SUPERSEDED (with a back-link to the
    new job id, if given). The new job itself is created by the caller/pipeline;
    this only records the supersession + link."""
    record = store.get_job(job_id)
    if record is None:
        raise InputValidationError(f"unknown job: {job_id}")
    if record.state not in _SUPERSEDABLE:
        raise InputValidationError(
            f"cannot supersede a {record.state.value} job ({job_id}); only "
            f"{sorted(s.value for s in _SUPERSEDABLE)} may be superseded"
        )

    # Void the old sign-off first (it no longer stands).
    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_SIGNOFF_INVALIDATED,
        job_id=job_id,
        actor=actor,
        extra={"superseded_from_state": record.state.value},
    )

    store.set_state(job_id, JobState.SUPERSEDED, updated_at=ts)

    audit.append(
        ts=ts,
        stage="signoff",
        event=EVENT_SUPERSEDED,
        job_id=job_id,
        actor=actor,
        extra={"new_job_id": new_job_id},
    )
    return JobState.SUPERSEDED
