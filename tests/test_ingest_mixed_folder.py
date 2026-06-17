"""Unit 10: mixed-folder material-pack ingest + completeness report."""

from __future__ import annotations

import json
from pathlib import Path

from lcp.adapters.crawler.base import SourceSpec
from lcp.adapters.crawler.ingest import LocalIngestCrawler
from lcp.core.models import AssetState, SourceType


def _spec(job_dir: Path, src: Path) -> SourceSpec:
    job_dir.mkdir(parents=True, exist_ok=True)
    return SourceSpec(
        job_id=job_dir.name, source_type=SourceType.LOCAL_DIR,
        job_dir=job_dir, local_dir=src,
    )


def _report(bundle):
    return json.loads((bundle.raw_dir / "ingest_report.json").read_text(encoding="utf-8"))


def test_mixed_pack_writes_completeness_report(tmp_path):
    src = tmp_path / "pack"
    src.mkdir()
    (src / "title.txt").write_text("T", encoding="utf-8")
    (src / "body.txt").write_text("B", encoding="utf-8")
    (src / "a.png").write_bytes(b"\x89PNGdata")
    (src / "v.mp4").write_bytes(b"\x00mp4")
    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j1", src))
    rep = _report(bundle)
    assert rep["imported_images"] == 1
    assert rep["imported_videos"] == 1
    assert rep["has_title"] and rep["has_body"]
    assert rep["complete"] is True


def test_unsupported_files_flagged_not_silently_dropped(tmp_path):
    src = tmp_path / "pack"
    src.mkdir()
    (src / "a.png").write_bytes(b"\x89PNGdata")
    (src / "notes.docx").write_bytes(b"zip-ish")
    (src / "archive.zip").write_bytes(b"PK\x03\x04")
    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j2", src))
    rep = _report(bundle)
    skipped_names = {s["name"] for s in rep["skipped"]}
    assert skipped_names == {"notes.docx", "archive.zip"}
    assert rep["complete"] is False


def test_empty_media_flagged_as_failed(tmp_path):
    src = tmp_path / "pack"
    src.mkdir()
    (src / "good.png").write_bytes(b"\x89PNGdata")
    (src / "empty.jpg").write_bytes(b"")
    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j3", src))
    rep = _report(bundle)
    failed_names = {f["name"] for f in rep["failed"]}
    assert "empty.jpg" in failed_names
    # the empty file was NOT written as a 0-byte asset
    assert not (bundle.raw_dir / "images" / "empty.jpg").exists()
    # the good image still imported
    assert rep["imported_images"] == 1


def test_subfolder_noted_as_skipped(tmp_path):
    src = tmp_path / "pack"
    src.mkdir()
    (src / "a.png").write_bytes(b"\x89PNGdata")
    (src / "extras").mkdir()
    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j4", src))
    rep = _report(bundle)
    assert any(s["name"] == "extras" for s in rep["skipped"])


def test_empty_media_does_not_consume_max_assets_slot(tmp_path):
    # An empty (FAILED) media file must not eat a max_assets slot and starve a
    # valid later file.
    src = tmp_path / "pack"
    src.mkdir()
    (src / "1empty.jpg").write_bytes(b"")          # sorts first, empty
    (src / "2good.png").write_bytes(b"\x89PNGdata")  # valid, must still import
    job_dir = tmp_path / "j6"
    job_dir.mkdir(parents=True, exist_ok=True)
    spec = SourceSpec(
        job_id="j6", source_type=SourceType.LOCAL_DIR,
        job_dir=job_dir, local_dir=src, max_assets=1,
    )
    bundle = LocalIngestCrawler().crawl(spec)
    rep = _report(bundle)
    assert rep["imported_images"] == 1  # the good one survived the cap
    assert rep["truncated_at_max_assets"] is False


def test_empty_folder_reports_incomplete(tmp_path):
    src = tmp_path / "pack"
    src.mkdir()
    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j5", src))
    rep = _report(bundle)
    assert rep["imported_images"] == 0 and rep["imported_videos"] == 0
    assert rep["has_body"] is False
