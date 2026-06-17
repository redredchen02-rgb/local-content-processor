"""De-watermark gate (plan Unit 8): attested-only, isolated, fail-closed.

Runs BEFORE normalization so cleaned assets re-enter the normal media gate. It
ONLY acts on a job with a valid attestation (Unit 7); without one it is a no-op
(default-locked). Each cleaned asset is marked in the manifest with PII-free
provenance (``watermark_removed`` + the evidence SHA-256). Engine failure /
low-confidence / out-of-scope → ``NEEDS_REVISION`` (never a silent partial).
Dry-run writes no cleaned output. Idempotent: a re-run over an already-cleaned
asset is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.config import InpaintConfig
from ...core.errors import ExternalServiceError
from ...core.models import AssetKind, AssetState
from ...core.state import JobState
from ..media.dewatermark_runner import DewatermarkRunner
from ..media.mask import write_box_mask
from ..publisher.dewatermark import DewatermarkAttestation
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from ..storage.manifest import read_manifest, write_manifest
from ._persist import persist_gate_state

EVENT_DEWATERMARK_GATE = "DEWATERMARK_GATE"


@dataclass(frozen=True)
class DewatermarkOutcome:
    job_state: JobState | None  # NEEDS_REVISION on failure; None on pass/no-op
    cleaned: int = 0
    report: dict[str, Any] = field(default_factory=dict)


def run_dewatermark_gate(
    *,
    job_id: str,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    attestation: DewatermarkAttestation | None,
    inpaint_config: InpaintConfig,
    runner: DewatermarkRunner | None = None,
    dry_run: bool = False,
    actor: str = "system",
) -> DewatermarkOutcome:
    """Clean attested job images in place before normalize. No-op when not
    attested or not enabled. Raises DependencyError (no engine) like the media
    gate; maps engine failure to NEEDS_REVISION."""
    # Default-locked: no attestation -> never de-watermark.
    if attestation is None or not inpaint_config.enabled:
        return DewatermarkOutcome(job_state=None, report={"status": "skipped"})

    manifest = read_manifest(store.job_dir(job_id))
    if manifest is None:
        return DewatermarkOutcome(job_state=None, report={"status": "no_manifest"})

    boxes = list(inpaint_config.default_boxes)
    if not boxes:
        # No mask region configured -> nothing to remove (honest no-op, not a
        # silent "cleaned"). Operator-drawn boxes wire in via the GUI (Unit 9).
        return DewatermarkOutcome(job_state=None, report={"status": "no_mask"})

    run = runner or DewatermarkRunner(inpaint_config)
    job_dir = store.job_dir(job_id)
    cleaned = 0
    failed = False
    new_assets = []
    for a in manifest.assets:
        if a.kind is not AssetKind.IMAGE or a.state is not AssetState.OK:
            new_assets.append(a)
            continue
        src = job_dir / a.path
        if dry_run:
            # dry-run: no engine call, no cleaned output written.
            new_assets.append(a)
            continue
        try:
            from PIL import Image

            with Image.open(src) as im:
                size = im.size
            mask_path = write_box_mask(size, boxes, job_dir / "processed" / "masks" / (Path(a.path).name + ".mask.png"))
            run.remove(src=src, mask=mask_path, dst=src)
        except ExternalServiceError:
            # low-confidence / engine failure / out-of-scope -> needs_revision,
            # NEVER a silent partial. Mark the asset and stop publishing it.
            failed = True
            new_assets.append(a.model_copy(update={
                "state": AssetState.NEEDS_REVISION,
                "note": "de-watermark failed/low-confidence",
            }))
            continue
        cleaned += 1
        new_assets.append(a.model_copy(update={
            "watermark_removed": True,
            "watermark_evidence_sha256": attestation.evidence_sha256,
        }))

    if not dry_run and (cleaned or failed):
        write_manifest(job_dir, manifest.model_copy(update={"assets": new_assets}), create_only=False)

    audit.append(
        ts=ts,
        stage="process",
        event=EVENT_DEWATERMARK_GATE,
        job_id=job_id,
        actor=actor,
        extra={
            "cleaned_count": cleaned,
            "failed": failed,
            "evidence_sha256": attestation.evidence_sha256,
            "dry_run": dry_run,
        },
    )

    if failed:
        persist_gate_state(store, job_id, JobState.NEEDS_REVISION, updated_at=ts)
        return DewatermarkOutcome(
            job_state=JobState.NEEDS_REVISION, cleaned=cleaned,
            report={"status": "needs_revision"},
        )
    return DewatermarkOutcome(job_state=None, cleaned=cleaned, report={"status": "ok"})
