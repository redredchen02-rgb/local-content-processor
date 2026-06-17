"""Media validation/normalization gate (Stage 2 wiring).

Image paths need no ffmpeg (Pillow only). The video path is exercised by
monkeypatching ffprobe so the spec/black judgement is deterministic and offline.
"""

from pathlib import Path

import pytest
from PIL import Image

from lcp.adapters.media import ffprobe
from lcp.adapters.processor import media_checker
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.adapters.storage.manifest import write_manifest
from lcp.core.config import MediaConfig
from lcp.core.models import AssetKind, AssetRef, AssetState, Manifest, SourceType
from lcp.core.state import JobState

TS = "2026-06-16T00:00:00Z"


def _sharp_jpeg(path: Path, size: tuple[int, int]) -> None:
    """A high-edge checkerboard (not blurry: high variance-of-Laplacian)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("L", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = 255 if (x // 4 + y // 4) % 2 else 0
    img.convert("RGB").save(path, format="JPEG", quality=95)


def _setup(tmp_path, assets):
    store = JobStore(base_dir=tmp_path)
    audit = AuditLog(Path(tmp_path) / "audit.jsonl")
    store.create_job("j", created_at=TS)
    store.set_state("j", JobState.CRAWLED, updated_at=TS)
    job_dir = store.job_dir("j")
    write_manifest(
        job_dir,
        Manifest(job_id="j", source_type=SourceType.URL, assets=assets),
    )
    return store, audit, job_dir


def _run(store, audit):
    return media_checker.run_media_gate(
        job_id="j", store=store, audit=audit, ts=TS, media_config=MediaConfig()
    )


def _img_asset(rel):
    return AssetRef(kind=AssetKind.IMAGE, path=rel, state=AssetState.OK)


# --- no media -> clean no-op (text-only article is valid) --------------------


def test_no_media_is_noop_pass(tmp_path):
    store, audit, job_dir = _setup(tmp_path, assets=[])
    out = _run(store, audit)
    assert out.job_state is None
    assert out.report["status"] == "pass"
    assert (job_dir / "processed" / "validation_report.json").exists()
    assert store.get_job("j").state is JobState.CRAWLED  # not parked


def test_no_manifest_is_noop_pass(tmp_path):
    store = JobStore(base_dir=tmp_path)
    audit = AuditLog(Path(tmp_path) / "audit.jsonl")
    store.create_job("j", created_at=TS)
    store.set_state("j", JobState.CRAWLED, updated_at=TS)
    out = _run(store, audit)
    assert out.job_state is None and out.report["status"] == "pass"


# --- valid image -> normalized + cover, pass ---------------------------------


def test_valid_image_normalized_and_cover_built(tmp_path):
    store, audit, job_dir = _setup(tmp_path, assets=[_img_asset("raw/images/a.jpg")])
    _sharp_jpeg(job_dir / "raw" / "images" / "a.jpg", (800, 450))
    out = _run(store, audit)
    assert out.job_state is None, out.report
    assert out.report["status"] == "pass"
    assert (job_dir / "processed" / "images" / "a.jpg").exists()
    assert (job_dir / "processed" / "cover" / "cover.jpg").exists()
    assert out.report["cover"] == "processed/cover/cover.jpg"
    assert store.get_job("j").state is JobState.CRAWLED


# --- quality miss -> NEEDS_REVISION (never BLOCKED) --------------------------


def test_too_small_image_parks_needs_revision(tmp_path):
    store, audit, job_dir = _setup(tmp_path, assets=[_img_asset("raw/images/tiny.jpg")])
    _sharp_jpeg(job_dir / "raw" / "images" / "tiny.jpg", (100, 100))  # < 640x360
    out = _run(store, audit)
    assert out.job_state is JobState.NEEDS_REVISION
    assert out.report["status"] == "needs_revision"
    assert store.get_job("j").state is JobState.NEEDS_REVISION
    entry = out.report["assets"][0]
    assert entry["state"] == "needs_revision"
    assert any("too small" in r for r in entry["reasons"])


def test_undecodable_image_is_failed_and_parks(tmp_path):
    store, audit, job_dir = _setup(tmp_path, assets=[_img_asset("raw/images/bad.jpg")])
    p = job_dir / "raw" / "images" / "bad.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"this is not an image")
    out = _run(store, audit)
    assert out.job_state is JobState.NEEDS_REVISION
    assert out.report["assets"][0]["state"] == "failed"
    assert store.get_job("j").state is JobState.NEEDS_REVISION


def test_missing_file_on_disk_is_failed(tmp_path):
    store, audit, job_dir = _setup(tmp_path, assets=[_img_asset("raw/images/ghost.jpg")])
    out = _run(store, audit)  # file never created
    assert out.job_state is JobState.NEEDS_REVISION
    assert out.report["assets"][0]["state"] == "failed"


# --- video path (ffprobe monkeypatched -> offline, deterministic) -----------


def test_video_spec_miss_parks_needs_revision(tmp_path, monkeypatch):
    store, audit, job_dir = _setup(
        tmp_path,
        assets=[AssetRef(kind=AssetKind.VIDEO, path="raw/videos/v.mp4", state=AssetState.OK)],
    )
    vp = job_dir / "raw" / "videos" / "v.mp4"
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # dummy; probe is monkeypatched

    monkeypatch.setattr(
        ffprobe, "probe",
        lambda *a, **k: ffprobe.VideoInfo(
            codec="vp9", width=1280, height=720, fps=30.0, bitrate_mbps=2.0
        ),
    )
    monkeypatch.setattr(ffprobe, "detect_black_segments", lambda *a, **k: [])
    out = _run(store, audit)
    assert out.job_state is JobState.NEEDS_REVISION
    entry = out.report["assets"][0]
    assert entry["kind"] == "video"
    assert any("codec" in r for r in entry["reasons"])  # vp9 != expected h264


def test_compliant_video_passes(tmp_path, monkeypatch):
    store, audit, job_dir = _setup(
        tmp_path,
        assets=[AssetRef(kind=AssetKind.VIDEO, path="raw/videos/ok.mp4", state=AssetState.OK)],
    )
    vp = job_dir / "raw" / "videos" / "ok.mp4"
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(
        ffprobe, "probe",
        lambda *a, **k: ffprobe.VideoInfo(
            codec="h264", width=1280, height=720, fps=30.0,
            bitrate_mbps=2.0, duration_s=10.0,
        ),
    )
    monkeypatch.setattr(ffprobe, "detect_black_segments", lambda *a, **k: [])
    out = _run(store, audit)
    assert out.job_state is None and out.report["status"] == "pass"


# --- end-to-end: Pipeline.process stops at the media gate --------------------


def test_pipeline_process_stops_at_media(tmp_path):
    from lcp.adapters.storage.draft_store import _read_source_text  # noqa: F401
    from lcp.core.config import Config
    from lcp.pipeline import Pipeline

    store, audit, job_dir = _setup(tmp_path, assets=[_img_asset("raw/images/tiny.jpg")])
    _sharp_jpeg(job_dir / "raw" / "images" / "tiny.jpg", (120, 120))  # too small
    (job_dir / "raw" / "source.txt").write_text("普通的中性事件描述。", encoding="utf-8")

    p = Pipeline(Config(), store, audit, dry_run=True)
    res = p.process("j", ts=TS, title="台北華山週末美食市集活動登場熱鬧滾滾人潮")
    assert res.final_state is JobState.NEEDS_REVISION
    assert res.stopped_at == "media"
    assert store.get_job("j").state is JobState.NEEDS_REVISION
