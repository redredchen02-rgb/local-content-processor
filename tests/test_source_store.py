"""Unit 2 + Unit 6: saved_sources PII-exception table CRUD + erasure."""

import os

import pytest

from lcp.adapters.storage.source_store import SourceStore
from lcp.core.errors import InputValidationError

TS = "2026-06-17T00:00:00Z"


def _store(tmp_path):
    return SourceStore(base_dir=tmp_path)


def test_db_file_is_0600_independent_of_umask(tmp_path):
    # U18: this store holds plaintext PII (source_ref/label) BY DESIGN, so the
    # shared lcp.db must be 0600 even if the startup umask were ever skipped.
    # A loose umask proves the explicit chmod is doing the work, not the umask.
    old = os.umask(0o000)
    try:
        s = _store(tmp_path)
    finally:
        os.umask(old)
    mode = os.stat(s.db_path).st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_add_and_list_roundtrip_preserves_plaintext(tmp_path):
    s = _store(tmp_path)
    src = s.add_source(label="厂商博客", source_ref="https://example.com/a", created_at=TS)
    rows = s.list_sources()
    assert len(rows) == 1
    # plaintext source_ref must be recoverable verbatim (reuse depends on it)
    assert rows[0].source_ref == "https://example.com/a"
    assert rows[0].label == "厂商博客"
    assert rows[0].id == src.id


def test_delete_by_id(tmp_path):
    s = _store(tmp_path)
    src = s.add_source(label="x", source_ref="https://e.com/1", created_at=TS)
    assert s.delete_source(src.id) is True
    assert s.list_sources() == []


def test_empty_label_or_source_ref_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(InputValidationError):
        s.add_source(label="  ", source_ref="https://e.com", created_at=TS)
    with pytest.raises(InputValidationError):
        s.add_source(label="x", source_ref="   ", created_at=TS)


def test_list_empty_returns_empty_no_error(tmp_path):
    assert _store(tmp_path).list_sources() == []


def test_delete_nonexistent_is_noop(tmp_path):
    s = _store(tmp_path)
    assert s.delete_source("does-not-exist") is False


def test_duplicate_id_rejected(tmp_path):
    s = _store(tmp_path)
    s.add_source(label="a", source_ref="https://e.com/a", created_at=TS, source_id="fixed")
    with pytest.raises(InputValidationError):
        s.add_source(label="b", source_ref="https://e.com/b", created_at=TS, source_id="fixed")


def test_persists_across_connections(tmp_path):
    # WAL: a second store instance (new connection) sees the committed row.
    _store(tmp_path).add_source(label="a", source_ref="https://e.com/a", created_at=TS)
    assert len(SourceStore(base_dir=tmp_path).list_sources()) == 1


def test_no_plaintext_leaks_into_audit_jsonl(tmp_path):
    # The CRUD path must never create/write audit.jsonl with the plaintext.
    s = _store(tmp_path)
    s.add_source(label="secret-note", source_ref="https://leak.example/x", created_at=TS)
    s.delete_by_source_ref("https://leak.example/x")
    audit = tmp_path / "audit.jsonl"
    if audit.exists():
        text = audit.read_text(encoding="utf-8")
        assert "leak.example" not in text
        assert "secret-note" not in text


def test_delete_by_source_ref_removes_all_matches(tmp_path):
    s = _store(tmp_path)
    s.add_source(label="a", source_ref="https://e.com/dup", created_at=TS)
    s.add_source(label="b", source_ref="https://e.com/dup", created_at=TS)
    s.add_source(label="c", source_ref="https://e.com/other", created_at=TS)
    removed = s.delete_by_source_ref("https://e.com/dup")
    assert removed == 2
    assert [r.source_ref for r in s.list_sources()] == ["https://e.com/other"]


def test_delete_all_wipes_table(tmp_path):
    s = _store(tmp_path)
    s.add_source(label="a", source_ref="https://e.com/a", created_at=TS)
    s.add_source(label="b", source_ref="https://e.com/b", created_at=TS)
    assert s.delete_all() == 2
    assert s.list_sources() == []
