import os

import pytest

from lcp.adapters.storage import manifest as M
from lcp.core.errors import InputValidationError
from lcp.core.models import AssetKind, AssetRef, Manifest, SourceType


def _manifest(job_id="j1", note=None):
    return Manifest(
        job_id=job_id,
        source_type=SourceType.URL,
        assets=[AssetRef(kind=AssetKind.IMAGE, path="raw/a.jpg", note=note)],
    )


def test_atomic_commit_writes_and_reads(tmp_path):
    written = M.write_manifest(tmp_path, _manifest())
    assert written is True
    got = M.read_manifest(tmp_path)
    assert got is not None and got.job_id == "j1"


def test_no_partial_file_left_on_disk(tmp_path):
    M.write_manifest(tmp_path, _manifest())
    # only manifest.json should exist, no stray temp files
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"manifest.json"}
    assert not any(n.startswith(".manifest.json.tmp") for n in names)


def test_deterministic_skip_on_unchanged(tmp_path):
    M.write_manifest(tmp_path, _manifest())
    mtime1 = os.stat(M.manifest_path(tmp_path)).st_mtime_ns
    # Same content again with deterministic_skip -> skipped (returns False).
    written = M.write_manifest(tmp_path, _manifest(), deterministic_skip=True)
    assert written is False
    mtime2 = os.stat(M.manifest_path(tmp_path)).st_mtime_ns
    assert mtime1 == mtime2  # file untouched


def test_deterministic_skip_rewrites_on_change(tmp_path):
    M.write_manifest(tmp_path, _manifest(note="old"))
    written = M.write_manifest(
        tmp_path, _manifest(note="new"), deterministic_skip=True
    )
    assert written is True
    assert M.read_manifest(tmp_path).assets[0].note == "new"


def test_content_hash_stable_and_changes(tmp_path):
    h1 = M.content_hash(_manifest(note="x"))
    h2 = M.content_hash(_manifest(note="x"))
    h3 = M.content_hash(_manifest(note="y"))
    assert h1 == h2
    assert h1 != h3


def test_create_only_refuses_overwrite(tmp_path):
    M.write_manifest(tmp_path, _manifest())
    with pytest.raises(InputValidationError):
        M.write_manifest(tmp_path, _manifest(note="other"), create_only=True)
