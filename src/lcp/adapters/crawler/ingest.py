"""Local material-folder ingest into the raw_job_bundle shape (no network).

A LOCAL_DIR source spec: copy a local folder's text + images + videos into
data/jobs/<id>/raw/ as the same bundle the Scrapy path produces, so Unit 5
consumes one shape regardless of origin. Every read path goes through
net_guard.safe_join so a folder containing `../` or an escaping symlink is
rejected (plan path-traversal). Downloaded/copied media land 0600.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ...core.errors import InputValidationError
from ...core.models import AssetKind, AssetRef, AssetState, SourceType
from ..storage.manifest import manifest_path, write_manifest
from . import net_guard
from .base import Crawler, RawJobBundle, SourceSpec
from .bundle import build_manifest, derive_status, sha256_bytes

# Recognised extensions -> AssetKind.
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_TEXT_NAMES = ("body.txt", "content.txt", "text.txt", "source.txt")
_TITLE_NAMES = ("title.txt",)


def _write_0600(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_CREAT|O_EXCL-free but enforce mode after write; umask 0077 at
    # startup already yields 0600, the chmod is belt-and-suspenders.
    with path.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _kind_for(path: Path) -> AssetKind | None:
    ext = path.suffix.lower()
    if ext in _IMAGE_EXT:
        return AssetKind.IMAGE
    if ext in _VIDEO_EXT:
        return AssetKind.VIDEO
    return None


class LocalIngestCrawler(Crawler):
    """Crawler implementation that ingests a local folder (no network).

    Audit/state wiring is the runner's job; this stays a pure folder->bundle
    transform so it is trivially testable without network or storage glue."""

    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        if spec.source_type is not SourceType.LOCAL_DIR or spec.local_dir is None:
            raise InputValidationError("LocalIngestCrawler requires a LOCAL_DIR spec")

        src = Path(spec.local_dir).resolve()
        if not src.is_dir():
            raise InputValidationError(f"material folder not found: {src}")

        # create_only must be SIDE-EFFECT-FREE on refusal: check for an existing
        # manifest at the TOP, before we write source.txt or copy any media (R11).
        # Otherwise a second ingest would clobber source.txt/media and THEN raise.
        if manifest_path(spec.job_dir).exists():
            raise InputValidationError(
                f"job bundle already exists for {spec.job_id}; refusing to "
                "overwrite (create_only)"
            )

        raw_dir = spec.job_dir / "raw"
        images_dir = raw_dir / "images"
        videos_dir = raw_dir / "videos"
        for d in (raw_dir, images_dir, videos_dir):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass

        # --- text extraction (title + body) ---
        title = self._read_named(src, _TITLE_NAMES)
        body = self._read_named(src, _TEXT_NAMES)

        # --- media assets + completeness tracking (Unit 10) ---
        # A material pack is mixed image/video/text in one folder. We never
        # SILENTLY drop a file: unrecognised types and empty/unreadable media are
        # recorded in a completeness report so the operator sees what was left
        # out (decode/playability validation proper stays in the media gate,
        # which already flags FAILED — we do the structural completeness here).
        assets: list[AssetRef] = []
        skipped: list[dict[str, str]] = []
        truncated = False
        count = 0
        for entry in sorted(src.iterdir()):
            if count >= spec.max_assets:
                truncated = True
                break
            # safe_join re-validates the member path stays within src (rejects
            # symlink-escape; resolve() catches a symlink pointing outside).
            try:
                member = net_guard.safe_join(src, entry.name)
            except InputValidationError:
                assets.append(
                    AssetRef(
                        kind=AssetKind.TEXT,
                        path=entry.name,
                        state=AssetState.FAILED,
                        note="path escapes material folder",
                    )
                )
                continue
            if not member.is_file():
                if member.is_dir():
                    skipped.append({"name": entry.name, "reason": "subfolder not scanned"})
                continue
            kind = _kind_for(member)
            if kind is None:
                # Not media — text inputs are handled separately; everything else
                # is an unsupported type the operator should know was excluded.
                if member.name not in _TEXT_NAMES and member.name not in _TITLE_NAMES:
                    skipped.append({"name": member.name, "reason": "unsupported file type"})
                continue
            try:
                data = member.read_bytes()
            except OSError as e:
                assets.append(
                    AssetRef(kind=kind, path=member.name, state=AssetState.FAILED, note=str(e))
                )
                continue
            if not data:
                # Empty media is unopenable/unplayable — flag, never write a 0-byte
                # asset the media gate would then have to fail. Does NOT consume a
                # max_assets slot (it produced no usable asset).
                assets.append(
                    AssetRef(
                        kind=kind, path=member.name, state=AssetState.FAILED,
                        note="empty file (unopenable)",
                    )
                )
                continue
            count += 1
            dest_dir = images_dir if kind is AssetKind.IMAGE else videos_dir
            dest = dest_dir / member.name
            _write_0600(dest, data)
            rel = dest.relative_to(spec.job_dir).as_posix()
            assets.append(
                AssetRef(
                    kind=kind,
                    path=rel,
                    sha256=sha256_bytes(data),
                    state=AssetState.OK,
                )
            )

        # --- persist source.txt (body) for the bundle ---
        source_text = body or ""
        _write_0600(raw_dir / "source.txt", source_text.encode("utf-8"))

        status = derive_status(title=title, body=body, assets=assets)
        manifest = build_manifest(
            job_id=spec.job_id,
            source_type=SourceType.LOCAL_DIR,
            source_domain=None,
            fetched_at=None,
            assets=assets,
            source_html=None,
            source_text=source_text,
            crawl_status=status,
        )
        # create_only: never clobber an existing job's bundle (plan R11).
        write_manifest(spec.job_dir, manifest, create_only=True)

        # Completeness report (Unit 10): what was imported vs flagged/skipped, so a
        # mixed pack with missing or unopenable items is visible, not silent. PII-
        # free: filenames are operator-chosen, not subject content.
        ok_images = sum(
            1 for a in assets if a.kind is AssetKind.IMAGE and a.state is AssetState.OK
        )
        ok_videos = sum(
            1 for a in assets if a.kind is AssetKind.VIDEO and a.state is AssetState.OK
        )
        failed = [
            {"name": Path(a.path).name, "reason": a.note or "failed"}
            for a in assets if a.state is AssetState.FAILED
        ]
        report = {
            "job_id": spec.job_id,
            "has_title": bool(title),
            "has_body": bool(body),
            "imported_images": ok_images,
            "imported_videos": ok_videos,
            "failed": failed,
            "skipped": skipped,
            "truncated_at_max_assets": truncated,
            "complete": not failed and not skipped and not truncated,
        }
        _write_0600(
            raw_dir / "ingest_report.json",
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        )

        return RawJobBundle(
            job_id=spec.job_id,
            raw_dir=raw_dir,
            manifest=manifest,
            job_status=status,
        )

    @staticmethod
    def _read_named(src: Path, names: tuple[str, ...]) -> str | None:
        for name in names:
            try:
                member = net_guard.safe_join(src, name)
            except InputValidationError:
                continue
            if member.is_file():
                try:
                    return member.read_text(encoding="utf-8").strip()
                except OSError:
                    return None
        return None
