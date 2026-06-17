"""Media validation + normalization gate (Stage 2 imperative shell).

Wires the (previously staged-but-unwired) media subsystem into Stage 2. It reads
the job's RAW manifest assets, normalizes images (-> processed/images/, 800px) and
composes a cover (-> processed/cover/cover.jpg), probes videos (ffprobe spec +
black detection), and asks the PURE core.rules.asset_rules for each
pass/needs_revision DECISION (adapters MEASURE, core JUDGES — plan Unit 5). It
writes processed/validation_report.json and, if any asset needs revision, parks
the job at NEEDS_REVISION.

A media QUALITY issue is NEVER BLOCKED — that tier is reserved for risk redlines
(asset_rules.Decision documents this). No media assets (or no raw manifest) makes
the gate a no-op (pass): a text-only article is valid.

dry_run note: media validation is deterministic LOCAL I/O (Pillow / ffprobe in a
hardened subprocess) — it runs even in dry-run (only the LLM call is skipped),
consistent with "the deterministic local stages still run"."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.config import MediaConfig
from ...core.errors import ExternalServiceError, InputValidationError
from ...core.models import AssetKind, AssetRef, AssetState
from ...core.rules import asset_rules
from ...core.state import JobState
from ..media import ffprobe, normalizer
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from ..storage.manifest import read_manifest
from ._persist import persist_gate_state

EVENT_MEDIA_GATE = "MEDIA_GATE"
_REPORT_NAME = "validation_report.json"


@dataclass(frozen=True)
class MediaGateOutcome:
    """What the gate did: per-asset report + the persisted state (if any)."""

    job_state: JobState | None  # None when all media OK / no media (caller continues)
    report: dict[str, Any] = field(default_factory=dict)


def _write_0600_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic 0600 write of the validation report (temp + fsync + chmod-before-
    replace), mirroring the rest of the storage layer."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _validate_images(
    images: list[AssetRef], job_dir: Path, media: MediaConfig
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Normalize + judge each OK image. Returns (per-asset entries, OK output
    paths for the cover, any_needs_revision)."""
    entries: list[dict[str, Any]] = []
    ok_outputs: list[str] = []
    needs_revision = False
    out_dir = job_dir / "processed" / "images"
    for a in images:
        entry: dict[str, Any] = {"kind": "image", "path": a.path}
        src = job_dir / a.path
        if not src.exists():
            entry.update(state=AssetState.FAILED.value, reasons=["file missing on disk"])
            entries.append(entry)
            needs_revision = True
            continue
        dst = out_dir / Path(a.path).name
        try:
            norm = normalizer.normalize_image(
                src, dst, max_width=media.image_width, quality=media.image_quality
            )
        except InputValidationError as e:
            # Decompression bomb / undecodable -> a per-asset failure, not a crash.
            entry.update(state=AssetState.FAILED.value, reasons=[str(e)])
            entries.append(entry)
            needs_revision = True
            continue
        entry.update(
            state=norm.decision.state.value,
            reasons=norm.decision.reasons,
            width=norm.width,
            height=norm.height,
            out=str(Path(norm.out_path).relative_to(job_dir)),
        )
        entries.append(entry)
        if norm.decision.ok:
            ok_outputs.append(norm.out_path)
        else:
            needs_revision = True
    return entries, ok_outputs, needs_revision


def _validate_videos(
    videos: list[AssetRef], job_dir: Path, media: MediaConfig
) -> tuple[list[dict[str, Any]], bool]:
    """Probe + judge each OK video (spec + black detection). Returns (per-asset
    entries, any_needs_revision). DependencyError (no ffmpeg) propagates."""
    entries: list[dict[str, Any]] = []
    needs_revision = False
    for a in videos:
        entry: dict[str, Any] = {"kind": "video", "path": a.path}
        src = job_dir / a.path
        if not src.exists():
            entry.update(state=AssetState.FAILED.value, reasons=["file missing on disk"])
            entries.append(entry)
            needs_revision = True
            continue
        try:
            info = ffprobe.probe(src)
        except ExternalServiceError as e:
            # A hostile/corrupt video (probe fail / timeout) -> flag, don't crash.
            entry.update(state=AssetState.NEEDS_REVISION.value, reasons=[f"probe failed: {e}"])
            entries.append(entry)
            needs_revision = True
            continue
        spec = asset_rules.judge_video(
            codec=info.codec,
            fps=info.fps,
            bitrate_mbps=info.bitrate_mbps,
            width=info.width,
            height=info.height,
            expected_codec=media.video_codec,
            min_bitrate_mbps=media.min_video_bitrate_mbps,
        )
        reasons = list(spec.reasons)
        try:
            black = ffprobe.detect_black_segments(src)
            reasons.extend(asset_rules.judge_black_segments(black, info.duration_s).reasons)
        except ExternalServiceError as e:
            reasons.append(f"blackdetect failed: {e}")
        state = AssetState.OK if not reasons else AssetState.NEEDS_REVISION
        entry.update(
            state=state.value, reasons=reasons, codec=info.codec, fps=info.fps,
            bitrate_mbps=info.bitrate_mbps, width=info.width, height=info.height,
        )
        entries.append(entry)
        if reasons:
            needs_revision = True
    return entries, needs_revision


def run_media_gate(
    *,
    job_id: str,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    media_config: MediaConfig,
    actor: str = "system",
) -> MediaGateOutcome:
    """Validate + normalize the job's media, write the validation report, and park
    the job at NEEDS_REVISION if any asset needs revision. No-op (pass) when the
    job has no OK image/video assets (or no manifest)."""
    job_dir = store.job_dir(job_id)
    manifest = read_manifest(job_dir)
    images: list[AssetRef] = []
    videos: list[AssetRef] = []
    if manifest is not None:
        images = [
            a for a in manifest.assets
            if a.kind == AssetKind.IMAGE and a.state == AssetState.OK
        ]
        videos = [
            a for a in manifest.assets
            if a.kind == AssetKind.VIDEO and a.state == AssetState.OK
        ]

    img_entries, ok_outputs, img_needs = _validate_images(images, job_dir, media_config)
    vid_entries, vid_needs = _validate_videos(videos, job_dir, media_config)
    needs_revision = img_needs or vid_needs

    cover_rel: str | None = None
    if ok_outputs:
        cover_path = normalizer.make_cover(
            ok_outputs[:4],
            job_dir / "processed" / "cover" / "cover.jpg",
            cover_width=media_config.cover_width,
            cover_height=media_config.cover_height,
            quality=media_config.image_quality,
        )
        try:
            os.chmod(cover_path, 0o600)
        except OSError:
            pass
        cover_rel = str(Path(cover_path).relative_to(job_dir))

    report = {
        "job_id": job_id,
        "image_count": len(images),
        "video_count": len(videos),
        "cover": cover_rel,
        "assets": img_entries + vid_entries,
        "status": "needs_revision" if needs_revision else "pass",
    }
    _write_0600_json(job_dir / "processed" / _REPORT_NAME, report)

    # PII-free audit: counts + status only (never asset paths / reasons text).
    nr_count = sum(
        1 for e in (img_entries + vid_entries)
        if e["state"] in (AssetState.NEEDS_REVISION.value, AssetState.FAILED.value)
    )
    audit.append(
        ts=ts,
        stage="process",
        event=EVENT_MEDIA_GATE,
        job_id=job_id,
        actor=actor,
        extra={
            "image_count": len(images),
            "video_count": len(videos),
            "needs_revision_count": nr_count,
            "has_cover": cover_rel is not None,
            "status": report["status"],
        },
    )

    job_state = None
    if needs_revision:
        job_state = JobState.NEEDS_REVISION
        persist_gate_state(store, job_id, job_state, updated_at=ts)
    return MediaGateOutcome(job_state=job_state, report=report)
