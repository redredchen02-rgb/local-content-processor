"""U5 — close the media fail-open: oversized / out-of-band-fps videos must park.

Two verified config seams were unwired in ``_validate_videos``:

* ``max_video_size_mb`` existed in ``MediaConfig`` but NOTHING checked the file
  size, so an arbitrarily large video passed the gate silently.
* ``judge_video`` accepts an fps band but the gate called it without forwarding
  any config, so the operator's fps knobs were dead (the asset_rules defaults
  governed regardless of config).

These tests pin the fixed behavior. The ffprobe probe is monkeypatched so the
spec/black judgement is deterministic and offline (mirrors test_media_checker).
"""

from pathlib import Path

from lcp.adapters.media import ffprobe
from lcp.adapters.processor import media_checker
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.adapters.storage.manifest import write_manifest
from lcp.core.config import MediaConfig
from lcp.core.models import AssetKind, AssetRef, AssetState, Manifest, SourceType
from lcp.core.state import JobState

TS = "2026-06-22T00:00:00Z"
_MIB = 1024 * 1024


def _setup(tmp_path, *, video_bytes: bytes):
    """A job with one OK video on disk (size = len(video_bytes))."""
    store = JobStore(base_dir=tmp_path)
    audit = AuditLog(Path(tmp_path) / "audit.jsonl")
    store.create_job("j", created_at=TS)
    store.set_state("j", JobState.CRAWLED, updated_at=TS)
    job_dir = store.job_dir("j")
    write_manifest(
        job_dir,
        Manifest(
            job_id="j",
            source_type=SourceType.URL,
            assets=[AssetRef(kind=AssetKind.VIDEO, path="raw/videos/v.mp4", state=AssetState.OK)],
        ),
    )
    vp = job_dir / "raw" / "videos" / "v.mp4"
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_bytes(video_bytes)
    return store, audit, job_dir


def _patch_probe(monkeypatch, *, fps: float | None = 30.0):
    """Make probe return an otherwise-compliant h264 video at the given fps so
    that SIZE or FPS is the only possible reason to park."""
    monkeypatch.setattr(
        ffprobe,
        "probe",
        lambda *a, **k: ffprobe.VideoInfo(
            codec="h264",
            width=1280,
            height=720,
            fps=fps,
            bitrate_mbps=2.0,
            duration_s=10.0,
        ),
    )
    monkeypatch.setattr(ffprobe, "detect_black_segments", lambda *a, **k: [])


def _run(store, audit, media: MediaConfig):
    return media_checker.run_media_gate(
        job_id="j", store=store, audit=audit, ts=TS, media_config=media
    )


# --- size: the real fail-open (currently passes; must park) ------------------


def test_oversized_video_parks_needs_revision(tmp_path, monkeypatch):
    """A video over ``max_video_size_mb`` must park. On unfixed code the size is
    never checked, so this video passes — this test proves the fail-open."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * (2 * _MIB))  # 2 MiB
    _patch_probe(monkeypatch)
    out = _run(store, audit, MediaConfig(max_video_size_mb=1))  # limit 1 MiB
    assert out.job_state is JobState.NEEDS_REVISION
    entry = out.report["assets"][0]
    assert entry["state"] == "needs_revision"
    assert any("large" in r or "size" in r for r in entry["reasons"])
    assert store.get_job("j").state is JobState.NEEDS_REVISION


def test_within_size_video_passes(tmp_path, monkeypatch):
    """A small, otherwise-compliant video passes (default 500MB limit)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    _patch_probe(monkeypatch)
    out = _run(store, audit, MediaConfig())
    assert out.job_state is None and out.report["status"] == "pass"


def test_size_exactly_at_limit_passes(tmp_path, monkeypatch):
    """Boundary: a file exactly at the limit passes (only STRICTLY over parks)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * (1 * _MIB))  # exactly 1 MiB
    _patch_probe(monkeypatch)
    out = _run(store, audit, MediaConfig(max_video_size_mb=1))
    assert out.job_state is None, out.report


# --- fps: the dead config knob (must be wired) -------------------------------


def test_fps_above_configured_band_parks(tmp_path, monkeypatch):
    """fps above the CONFIGURED max parks — even when the asset_rules default
    band (24-61) would have allowed it (proves the config knob is wired)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    _patch_probe(monkeypatch, fps=50.0)  # within default 24-61, outside config 24-40
    out = _run(store, audit, MediaConfig(min_video_fps=24.0, max_video_fps=40.0))
    assert out.job_state is JobState.NEEDS_REVISION
    assert any("fps" in r for r in out.report["assets"][0]["reasons"])


def test_fps_below_configured_band_parks(tmp_path, monkeypatch):
    """fps below the configured min parks (default min is 24)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    _patch_probe(monkeypatch, fps=20.0)
    out = _run(store, audit, MediaConfig())
    assert out.job_state is JobState.NEEDS_REVISION
    assert any("fps" in r for r in out.report["assets"][0]["reasons"])


def test_fps_within_configured_band_passes(tmp_path, monkeypatch):
    """fps inside the configured band passes (config governs, not just defaults)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    _patch_probe(monkeypatch, fps=30.0)
    out = _run(store, audit, MediaConfig(min_video_fps=24.0, max_video_fps=40.0))
    assert out.job_state is None and out.report["status"] == "pass"


def test_unknown_fps_parks(tmp_path, monkeypatch):
    """A missing fps fact is fail-closed (cannot confirm the video meets spec)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    _patch_probe(monkeypatch, fps=None)
    out = _run(store, audit, MediaConfig())
    assert out.job_state is JobState.NEEDS_REVISION
    assert any("fps" in r for r in out.report["assets"][0]["reasons"])


def test_fps_at_max_boundary_passes(tmp_path, monkeypatch):
    """fps exactly == the configured max passes (inclusive band, mirrors the
    size '== limit passes' boundary)."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    _patch_probe(monkeypatch, fps=40.0)
    out = _run(store, audit, MediaConfig(min_video_fps=24.0, max_video_fps=40.0))
    assert out.job_state is None and out.report["status"] == "pass"


# --- fail-closed on a present-but-unstattable file (not a crash) -------------


def test_unstattable_video_parks_not_crash(tmp_path, monkeypatch):
    """A video that exists but whose stat() raises (e.g. EACCES) must PARK at the
    asset level — never escape as an uncaught OSError that crashes Stage 2."""
    store, audit, _ = _setup(tmp_path, video_bytes=b"\x00" * 4096)
    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if self.name == "v.mp4":
            raise PermissionError("stat denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)
    out = _run(store, audit, MediaConfig())  # probe never reached — guard returns first
    assert out.job_state is JobState.NEEDS_REVISION
    assert any("unreadable" in r for r in out.report["assets"][0]["reasons"])


# --- per-asset reason isolation across multiple videos -----------------------


def test_mixed_oversized_and_compliant_videos(tmp_path, monkeypatch):
    """One oversized + one compliant video: the job parks, but the size reason
    stays on the oversized asset only (no leak onto the compliant one)."""
    store = JobStore(base_dir=tmp_path)
    audit = AuditLog(Path(tmp_path) / "audit.jsonl")
    store.create_job("j", created_at=TS)
    store.set_state("j", JobState.CRAWLED, updated_at=TS)
    job_dir = store.job_dir("j")
    write_manifest(
        job_dir,
        Manifest(
            job_id="j",
            source_type=SourceType.URL,
            assets=[
                AssetRef(kind=AssetKind.VIDEO, path="raw/videos/big.mp4", state=AssetState.OK),
                AssetRef(kind=AssetKind.VIDEO, path="raw/videos/ok.mp4", state=AssetState.OK),
            ],
        ),
    )
    vdir = job_dir / "raw" / "videos"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "big.mp4").write_bytes(b"\x00" * (2 * _MIB))
    (vdir / "ok.mp4").write_bytes(b"\x00" * 4096)
    _patch_probe(monkeypatch)  # both probe as compliant h264/30fps

    out = _run(store, audit, MediaConfig(max_video_size_mb=1))
    assert out.job_state is JobState.NEEDS_REVISION
    by_path = {e["path"]: e for e in out.report["assets"]}
    assert any("large" in r for r in by_path["raw/videos/big.mp4"]["reasons"])
    assert by_path["raw/videos/ok.mp4"]["reasons"] == []
    assert by_path["raw/videos/ok.mp4"]["state"] == "ok"
