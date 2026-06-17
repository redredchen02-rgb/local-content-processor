"""De-watermark segregation-of-duties attestation (plan Unit 7, Batch 2).

De-watermarking is DEFAULT-LOCKED and only ever runs on owned/licensed assets,
behind a bounded, re-ratified amendment to R2 (the absolute "never mask the
rights-holder's source" rule). Unlock requires ALL of:

  (a) a SUBMITTER recorded at request time (party 1), and
  (b) a verifiable license-evidence reference (contract id / URL / ownership
      proof) — operator-asserted, NOT machine-verified, and
  (c) approval by a WHITELISTED reviewer who is NOT the submitter (party 2).

This is NET-NEW plumbing: the pipeline had no submitter/approver split. The
honest consequence on a single-OS-account laptop is that the submitter and the
only available reviewer are the same person → de-watermark stays LOCKED until a
real second party approves. That is the intended fail-closed behaviour, not a
bug. A verbatim DEWATERMARK_DISCLAIMER ("attestation, not authentication") is
recorded with every attestation. The raw evidence reference is written to a 0600
operator file; only its SHA-256 goes into the PII-free audit/index.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.config import Config
from ...core.errors import InputValidationError
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from .signoff import _require_whitelisted, observed_os_user

EVENT_DEWATERMARK_REQUESTED = "DEWATERMARK_REQUESTED"
EVENT_DEWATERMARK_ATTESTED = "DEWATERMARK_ATTESTED"

_REQUEST_NAME = "dewatermark_request.json"
_ATTEST_NAME = "dewatermark_attestation.json"

# VERBATIM — recorded with every attestation. Do not paraphrase: it states the
# honest limit that this records responsibility, it does not prove ownership nor
# prevent infringement (mirror signoff.DISCLAIMER).
DEWATERMARK_DISCLAIMER = (
    "ATTESTATION, NOT AUTHENTICATION: this records that a named submitter "
    "asserted an owned/licensed right to remove a watermark and that an "
    "independent whitelisted reviewer approved it. It is NOT proof of ownership, "
    "does NOT verify the license, and does NOT prevent infringement. The license "
    "evidence is operator-asserted; the system never fetches or validates it."
)


@dataclass(frozen=True)
class DewatermarkAttestation:
    """The persisted attestation the de-watermark engine gate (Unit 8) checks."""

    job_id: str
    submitter: str
    reviewer: str
    evidence_sha256: str
    attested: bool = True
    disclaimer: str = DEWATERMARK_DISCLAIMER


def _review_dir(store: JobStore, job_id: str) -> Path:
    d = store.job_dir(job_id) / "review"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_0600_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def request_dewatermark(
    job_id: str,
    submitter: str,
    *,
    store: JobStore,
    audit: AuditLog,
    ts: str,
) -> str:
    """Record the SUBMITTER (party 1) of a de-watermark request at request time.

    `submitter` is a person identifier (the operator requesting removal). We also
    stamp the observed OS user. Returns the recorded submitter. This must precede
    attestation so the reviewer ≠ submitter check has a submitter to compare to."""
    sub = (submitter or "").strip()
    if not sub:
        raise InputValidationError("de-watermark submitter is required")
    observed = observed_os_user()
    _write_0600_json(
        _review_dir(store, job_id) / _REQUEST_NAME,
        {"job_id": job_id, "submitter": sub, "observed_os_user": observed, "ts": ts},
    )
    audit.append(
        ts=ts,
        stage="dewatermark",
        event=EVENT_DEWATERMARK_REQUESTED,
        job_id=job_id,
        actor=observed,
        extra={"submitter": sub, "observed_os_user": observed},
    )
    return sub


def read_submitter(store: JobStore, job_id: str) -> str | None:
    path = _review_dir(store, job_id) / _REQUEST_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sub = data.get("submitter")
    return sub if isinstance(sub, str) and sub else None


def attest_dewatermark(
    job_id: str,
    reviewer: str,
    evidence_ref: str,
    *,
    config: Config,
    store: JobStore,
    audit: AuditLog,
    ts: str,
) -> DewatermarkAttestation:
    """Approve a de-watermark (party 2). Requires a recorded submitter + a
    whitelisted reviewer ≠ submitter + a non-empty evidence reference.

    The raw evidence reference is written to a 0600 operator file; only its
    SHA-256 enters the audit (PII-free). Fail-closed: any missing/garbage input,
    or reviewer == submitter, raises and writes NO attestation — de-watermark
    stays locked."""
    submitter = read_submitter(store, job_id)
    if not submitter:
        raise InputValidationError(
            f"no de-watermark submitter recorded for {job_id}; call "
            "request_dewatermark first (the submitter must differ from the reviewer)"
        )
    rev = (reviewer or "").strip()
    evidence = (evidence_ref or "").strip()
    if not evidence:
        raise InputValidationError(
            "a verifiable license-evidence reference is required (contract id / "
            "URL / ownership proof); it is operator-asserted, never auto-verified"
        )
    _require_whitelisted(config, rev)
    if rev == submitter:
        raise InputValidationError(
            f"segregation of duties: the de-watermark reviewer ({rev!r}) must be "
            f"INDEPENDENT of the submitter ({submitter!r})"
        )

    evidence_sha = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    # Raw evidence -> 0600 operator file (not the PII-free index/audit).
    with (_review_dir(store, job_id) / "dewatermark_evidence.txt").open(
        "w", encoding="utf-8"
    ) as f:
        f.write(evidence + "\n")
    evid_path = _review_dir(store, job_id) / "dewatermark_evidence.txt"
    try:
        os.chmod(evid_path, 0o600)
    except OSError:
        pass

    _write_0600_json(
        _review_dir(store, job_id) / _ATTEST_NAME,
        {
            "job_id": job_id,
            "submitter": submitter,
            "reviewer": rev,
            "evidence_sha256": evidence_sha,
            "attested": True,
            "disclaimer": DEWATERMARK_DISCLAIMER,
            "ts": ts,
        },
    )
    audit.append(
        ts=ts,
        stage="dewatermark",
        event=EVENT_DEWATERMARK_ATTESTED,
        job_id=job_id,
        actor=observed_os_user(),
        artifact_sha256=evidence_sha,
        extra={
            "submitter": submitter,
            "reviewer_stated": rev,
            "observed_os_user": observed_os_user(),
            "evidence_sha256": evidence_sha,
            "disclaimer": DEWATERMARK_DISCLAIMER,
        },
    )
    return DewatermarkAttestation(
        job_id=job_id,
        submitter=submitter,
        reviewer=rev,
        evidence_sha256=evidence_sha,
    )


def read_attestation(store: JobStore, job_id: str) -> DewatermarkAttestation | None:
    """Return the persisted attestation, or None if the job is NOT attested
    (de-watermark stays locked). Unit 8's gate calls this."""
    path = _review_dir(store, job_id) / _ATTEST_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("attested"):
        return None
    return DewatermarkAttestation(
        job_id=job_id,
        submitter=str(data.get("submitter", "")),
        reviewer=str(data.get("reviewer", "")),
        evidence_sha256=str(data.get("evidence_sha256", "")),
    )
