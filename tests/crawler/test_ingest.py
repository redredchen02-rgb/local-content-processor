"""Local-folder ingest -> raw_job_bundle, no network (plan happy/edge paths)."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from lcp.adapters.crawler.base import (
    STATUS_CRAWLED,
    STATUS_NEEDS_REVISION,
    SourceSpec,
)
from lcp.adapters.crawler.ingest import LocalIngestCrawler
from lcp.adapters.storage.manifest import read_manifest
from lcp.core.errors import InputValidationError
from lcp.core.models import AssetKind, AssetState, SourceType


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard guard: ingest must NOT touch the network."""

    def _boom(*a, **k):
        raise AssertionError("ingest attempted a network call")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)


def _spec(job_dir: Path, src: Path) -> SourceSpec:
    job_dir.mkdir(parents=True, exist_ok=True)
    return SourceSpec(
        job_id=job_dir.name,
        source_type=SourceType.LOCAL_DIR,
        job_dir=job_dir,
        local_dir=src,
    )


def test_happy_ingest_produces_bundle_0600_and_sha256(tmp_path):
    src = tmp_path / "material"
    src.mkdir()
    (src / "title.txt").write_text("My Title", encoding="utf-8")
    (src / "body.txt").write_text("The body text.", encoding="utf-8")
    (src / "img1.png").write_bytes(b"\x89PNGdata1")
    (src / "clip.mp4").write_bytes(b"\x00\x00\x00mp4data")

    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j1", src))

    assert bundle.job_status == STATUS_CRAWLED
    # source.txt written
    src_txt = bundle.raw_dir / "source.txt"
    assert src_txt.read_text(encoding="utf-8") == "The body text."

    job_dir = bundle.raw_dir.parent  # asset.path is relative to job_dir
    kinds = {a.kind for a in bundle.assets if a.state is AssetState.OK}
    assert AssetKind.IMAGE in kinds and AssetKind.VIDEO in kinds
    for a in bundle.assets:
        assert a.sha256 and len(a.sha256) == 64
        # downloaded media is 0600
        full = job_dir / a.path
        mode = os.stat(full).st_mode & 0o777
        assert mode & 0o077 == 0

    # manifest persisted with assets + hashes
    m = read_manifest(job_dir)
    assert m is not None
    assert m.source_type is SourceType.LOCAL_DIR
    assert m.hashes.source_text_sha256 is not None
    assert len(m.assets) == 2


def test_body_missing_needs_revision(tmp_path):
    src = tmp_path / "material"
    src.mkdir()
    (src / "title.txt").write_text("Has a title", encoding="utf-8")
    # no body.txt
    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j2", src))
    assert bundle.job_status == STATUS_NEEDS_REVISION


def test_no_clobber_existing_job(tmp_path):
    src = tmp_path / "material"
    src.mkdir()
    (src / "title.txt").write_text("t", encoding="utf-8")
    (src / "body.txt").write_text("b", encoding="utf-8")
    spec = _spec(tmp_path / "j3", src)
    LocalIngestCrawler().crawl(spec)
    # second ingest into the SAME job dir must refuse to overwrite the manifest
    spec2 = _spec(tmp_path / "j3", src)
    with pytest.raises(InputValidationError):
        LocalIngestCrawler().crawl(spec2)


def test_create_only_refusal_is_side_effect_free(tmp_path):
    """P3 regression: a second ingest on an existing job must NOT mutate
    source.txt (or copy media) before raising — the refusal is checked at the
    TOP, so the original bundle is untouched."""
    src = tmp_path / "material"
    src.mkdir()
    (src / "title.txt").write_text("first title", encoding="utf-8")
    (src / "body.txt").write_text("the original body", encoding="utf-8")
    job_dir = tmp_path / "j6"
    bundle = LocalIngestCrawler().crawl(_spec(job_dir, src))
    src_txt = bundle.raw_dir / "source.txt"
    before = src_txt.read_text(encoding="utf-8")
    assert before == "the original body"

    # A second ingest with DIFFERENT material must raise WITHOUT touching the
    # existing source.txt (would-be clobber happens before the manifest write).
    src2 = tmp_path / "material2"
    src2.mkdir()
    (src2 / "title.txt").write_text("second title", encoding="utf-8")
    (src2 / "body.txt").write_text("TAMPERED BODY THAT MUST NOT BE WRITTEN",
                                   encoding="utf-8")
    with pytest.raises(InputValidationError):
        LocalIngestCrawler().crawl(_spec(job_dir, src2))

    # source.txt is unchanged — the refusal had no side effects.
    assert src_txt.read_text(encoding="utf-8") == before


def test_missing_material_folder_rejected(tmp_path):
    spec = _spec(tmp_path / "j4", tmp_path / "does-not-exist")
    with pytest.raises(InputValidationError):
        LocalIngestCrawler().crawl(spec)


def test_symlink_escape_member_marked_failed(tmp_path):
    src = tmp_path / "material"
    src.mkdir()
    (src / "title.txt").write_text("t", encoding="utf-8")
    (src / "body.txt").write_text("b", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"\x89PNGsecret")
    # a symlink inside the folder pointing at an external dir; safe_join must
    # reject members reached via it.
    os.symlink(outside / "secret.png", src / "escape.png")

    bundle = LocalIngestCrawler().crawl(_spec(tmp_path / "j5", src))
    # the escaping symlink is recorded FAILED, not copied in as OK
    failed = [a for a in bundle.assets if a.state is AssetState.FAILED]
    assert any("escape" in (a.note or "") or a.path == "escape.png" for a in failed)
    # and the secret is not present as an OK image asset
    ok_paths = [a.path for a in bundle.assets if a.state is AssetState.OK]
    assert all("secret" not in p for p in ok_paths)
