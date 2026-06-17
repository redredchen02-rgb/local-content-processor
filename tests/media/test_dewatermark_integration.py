"""Unit 8: isolated de-watermark runner + gate (attested-only, fail-closed)."""

from __future__ import annotations

import types

import pytest
from PIL import Image

from lcp.adapters.media.dewatermark_runner import DewatermarkRunner
from lcp.adapters.media.mask import build_box_mask
from lcp.adapters.processor.dewatermark_gate import run_dewatermark_gate
from lcp.adapters.publisher.dewatermark import DewatermarkAttestation
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.adapters.storage.manifest import read_manifest, write_manifest
from lcp.core.config import InpaintConfig
from lcp.core.errors import DependencyError, ExternalServiceError
from lcp.core.models import AssetKind, AssetRef, AssetState, Manifest, SourceType
from lcp.core.state import JobState

TS = "2026-06-17T00:00:00Z"
ATT = DewatermarkAttestation(job_id="j1", submitter="bob", reviewer="alice", evidence_sha256="e" * 64)


def _fake_engine(write_output=True, rc=0):
    """A subprocess_runner stub that 'cleans' by writing a tiny JPEG to --output."""
    def _run(cmd, **kwargs):
        out = cmd[cmd.index("--output") + 1]
        if write_output:
            Image.new("RGB", (40, 30), (10, 20, 30)).save(out, format="JPEG")
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
    return _run


# --- mask ---------------------------------------------------------------------


def test_build_box_mask_paints_white():
    m = build_box_mask((50, 40), [(5, 5, 15, 15)])
    assert m.mode == "L"
    assert m.getpixel((10, 10)) == 255
    assert m.getpixel((30, 30)) == 0


# --- runner -------------------------------------------------------------------


def test_runner_default_locked_raises(tmp_path):
    runner = DewatermarkRunner(InpaintConfig())  # no engine_cmd
    src = tmp_path / "a.jpg"
    Image.new("RGB", (20, 20)).save(src)
    mask = tmp_path / "m.png"
    build_box_mask((20, 20), [(1, 1, 5, 5)]).save(mask)
    with pytest.raises(DependencyError):
        runner.remove(src=src, mask=mask, dst=tmp_path / "out.jpg")


def test_runner_runs_engine_and_strips_exif(tmp_path):
    cfg = InpaintConfig(enabled=True, engine_cmd=["fake"])
    runner = DewatermarkRunner(cfg, subprocess_runner=_fake_engine())
    src = tmp_path / "a.jpg"
    Image.new("RGB", (40, 30)).save(src)
    mask = tmp_path / "m.png"
    build_box_mask((40, 30), [(1, 1, 5, 5)]).save(mask)
    out = runner.remove(src=src, mask=mask, dst=tmp_path / "out.jpg")
    img = Image.open(out)
    assert img.mode == "RGB"
    assert not img.getexif()  # EXIF stripped


def test_runner_nonzero_exit_is_external_error(tmp_path):
    cfg = InpaintConfig(enabled=True, engine_cmd=["fake"])
    runner = DewatermarkRunner(cfg, subprocess_runner=_fake_engine(rc=1))
    src = tmp_path / "a.jpg"
    Image.new("RGB", (20, 20)).save(src)
    mask = tmp_path / "m.png"
    build_box_mask((20, 20), [(1, 1, 5, 5)]).save(mask)
    with pytest.raises(ExternalServiceError):
        runner.remove(src=src, mask=mask, dst=tmp_path / "out.jpg")


# --- gate ---------------------------------------------------------------------


def _job_with_image(tmp_path, job_id="j1", *, processing=False):
    store = JobStore(base_dir=str(tmp_path))
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    if processing:
        store.mark_processing(job_id)  # gate runs as a Stage-2 step
    job_dir = store.job_dir(job_id)
    img_dir = job_dir / "raw" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), (200, 100, 50)).save(img_dir / "a.jpg")
    manifest = Manifest(
        job_id=job_id, source_type=SourceType.LOCAL_DIR,
        assets=[AssetRef(kind=AssetKind.IMAGE, path="raw/images/a.jpg", state=AssetState.OK)],
    )
    write_manifest(job_dir, manifest, create_only=False)
    return store


def _cfg(**kw):
    return InpaintConfig(enabled=True, engine_cmd=["fake"], default_boxes=[(40, 25, 58, 38)], **kw)


def test_gate_noop_without_attestation(tmp_path):
    store = _job_with_image(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    out = run_dewatermark_gate(
        job_id="j1", store=store, audit=audit, ts=TS, attestation=None,
        inpaint_config=_cfg(), runner=DewatermarkRunner(_cfg(), subprocess_runner=_fake_engine()),
    )
    assert out.job_state is None and out.cleaned == 0


def test_gate_cleans_attested_and_marks_provenance(tmp_path):
    store = _job_with_image(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    out = run_dewatermark_gate(
        job_id="j1", store=store, audit=audit, ts=TS, attestation=ATT,
        inpaint_config=_cfg(), runner=DewatermarkRunner(_cfg(), subprocess_runner=_fake_engine()),
    )
    assert out.cleaned == 1 and out.job_state is None
    m = read_manifest(store.job_dir("j1"))
    asset = m.assets[0]
    assert asset.watermark_removed is True
    assert asset.watermark_evidence_sha256 == ATT.evidence_sha256


def test_gate_engine_failure_is_needs_revision_no_silent_partial(tmp_path):
    store = _job_with_image(tmp_path, processing=True)
    audit = AuditLog(tmp_path / "audit.jsonl")
    out = run_dewatermark_gate(
        job_id="j1", store=store, audit=audit, ts=TS, attestation=ATT,
        inpaint_config=_cfg(),
        runner=DewatermarkRunner(_cfg(), subprocess_runner=_fake_engine(rc=1)),
    )
    assert out.job_state is JobState.NEEDS_REVISION
    m = read_manifest(store.job_dir("j1"))
    assert m.assets[0].state is AssetState.NEEDS_REVISION
    assert m.assets[0].watermark_removed is False


def test_gate_dry_run_writes_nothing(tmp_path):
    store = _job_with_image(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    before = (store.job_dir("j1") / "raw" / "images" / "a.jpg").read_bytes()
    out = run_dewatermark_gate(
        job_id="j1", store=store, audit=audit, ts=TS, attestation=ATT,
        inpaint_config=_cfg(), dry_run=True,
        runner=DewatermarkRunner(_cfg(), subprocess_runner=_fake_engine()),
    )
    assert out.cleaned == 0
    after = (store.job_dir("j1") / "raw" / "images" / "a.jpg").read_bytes()
    assert before == after  # untouched in dry-run


def test_gate_no_mask_configured_is_noop(tmp_path):
    store = _job_with_image(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    cfg = InpaintConfig(enabled=True, engine_cmd=["fake"])  # no default_boxes
    out = run_dewatermark_gate(
        job_id="j1", store=store, audit=audit, ts=TS, attestation=ATT,
        inpaint_config=cfg, runner=DewatermarkRunner(cfg, subprocess_runner=_fake_engine()),
    )
    assert out.job_state is None and out.cleaned == 0
    assert out.report["status"] == "no_mask"
