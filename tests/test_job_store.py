import os
import sqlite3

import pytest

from lcp.adapters.storage.job_store import JobStore
from lcp.core.errors import InputValidationError
from lcp.core.state import JobState, ReviewReason

TS = "2026-06-16T00:00:00Z"


def _store(tmp_path):
    return JobStore(base_dir=tmp_path)


def test_create_and_get(tmp_path):
    s = _store(tmp_path)
    rec = s.create_job("j1", created_at=TS)
    assert rec.state is JobState.NEW
    got = s.get_job("j1")
    assert got is not None and got.job_id == "j1"
    # job dir layout created
    d = s.job_dir("j1")
    for sub in ("raw", "processed", "review"):
        assert (d / sub).is_dir()


def test_get_missing_returns_none(tmp_path):
    assert _store(tmp_path).get_job("nope") is None


def test_create_duplicate_rejected(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    with pytest.raises(InputValidationError):
        s.create_job("j1", created_at=TS)


def test_set_state_validates_transition(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    # illegal jump should raise
    with pytest.raises(InputValidationError):
        s.set_state("j1", JobState.APPROVED, updated_at=TS)


def test_set_state_unknown_job(tmp_path):
    with pytest.raises(InputValidationError):
        _store(tmp_path).set_state("ghost", JobState.CRAWLED, updated_at=TS)


def test_processing_not_persisted(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    with pytest.raises(InputValidationError):
        s.set_state("j1", JobState.PROCESSING, updated_at=TS)
    # state in DB is still CRAWLED, never PROCESSING
    assert s.get_job("j1").state is JobState.CRAWLED


def test_create_with_processing_rejected(tmp_path):
    with pytest.raises(InputValidationError):
        _store(tmp_path).create_job("j1", created_at=TS, state=JobState.PROCESSING)


def test_processing_marker_file(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    assert not s.is_processing("j1")
    s.mark_processing("j1")
    assert s.is_processing("j1")
    s.clear_processing("j1")
    assert not s.is_processing("j1")


def test_list_by_state(tmp_path):
    s = _store(tmp_path)
    s.create_job("a", created_at="2026-06-16T00:00:01Z")
    s.create_job("b", created_at="2026-06-16T00:00:02Z")
    s.create_job("c", created_at="2026-06-16T00:00:03Z")
    s.set_state("a", JobState.CRAWLED, updated_at=TS)
    new_jobs = s.list_by_state(JobState.NEW)
    assert [r.job_id for r in new_jobs] == ["b", "c"]
    assert [r.job_id for r in s.list_by_state(JobState.CRAWLED)] == ["a"]


def test_review_reason_stored_as_code(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    # NEEDS_HUMAN_REVIEW needs to come from PROCESSING resting path; emulate by
    # validating reason persistence on a legal edge that allows resting.
    # CRAWLED -> CRAWLED_WARN is legal and resting.
    rec = s.set_state(
        "j1", JobState.CRAWLED_WARN, updated_at=TS, review_reason=ReviewReason.DEDUP
    )
    assert rec.review_reason is ReviewReason.DEDUP
    assert s.get_job("j1").review_reason is ReviewReason.DEDUP


def test_wal_mode_enabled(tmp_path):
    s = _store(tmp_path)
    conn = sqlite3.connect(s.db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_concurrent_connections_no_corruption(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    # Two independent connections write + read back.
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    c1 = s._connect()
    c2 = s._connect()
    try:
        c1.execute(
            "UPDATE jobs SET error_code = ? WHERE job_id = ?", ("E1", "j1")
        )
        c1.commit()
        row = c2.execute(
            "SELECT error_code FROM jobs WHERE job_id = ?", ("j1",)
        ).fetchone()
        assert row[0] == "E1"
    finally:
        c1.close()
        c2.close()
    assert s.get_job("j1").error_code == "E1"


def test_sqlite_schema_is_pii_free(tmp_path):
    s = _store(tmp_path)
    conn = sqlite3.connect(s.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    finally:
        conn.close()
    prohibited = {"title", "body", "text", "source_url", "url", "author", "domain"}
    assert cols & prohibited == set()
    assert "review_reason" in cols  # present, but stores enum code only


def test_job_dir_permissions_restrictive(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    mode = os.stat(s.job_dir("j1")).st_mode & 0o777
    assert mode & 0o077 == 0  # no group/other access


def test_list_all_and_counts_by_state(tmp_path):
    """Batch-2 perf: list_all() / counts_by_state() do one query each."""
    s = _store(tmp_path)
    for jid in ("a", "b", "c"):
        s.create_job(jid, created_at=TS)
    s.set_state("b", JobState.CRAWLED, updated_at=TS)
    # list_all returns every persisted job, ordered, no transient states
    allrecs = s.list_all()
    assert [r.job_id for r in allrecs] == ["a", "b", "c"]
    states = {r.job_id: r.state for r in allrecs}
    assert states["b"] is JobState.CRAWLED and states["a"] is JobState.NEW
    # counts_by_state groups
    counts = s.counts_by_state()
    assert counts == {"new": 2, "crawled": 1}


def test_persist_from_processing_validates_and_persists(tmp_path):
    """persist_from_processing drives a legal PROCESSING->target in one conn,
    refuses a transient target, and refuses an illegal predecessor."""
    s = _store(tmp_path)
    s.create_job("j", created_at=TS)
    s.set_state("j", JobState.CRAWLED, updated_at=TS)  # legal PROCESSING predecessor
    rec = s.persist_from_processing("j", JobState.BLOCKED, updated_at=TS,
                                    review_reason=ReviewReason.RISK)
    assert rec.state is JobState.BLOCKED
    assert s.get_job("j").state is JobState.BLOCKED
    assert not s.is_processing("j")  # marker cleared
    # transient target refused
    with pytest.raises(InputValidationError):
        s.persist_from_processing("j", JobState.PROCESSING, updated_at=TS)
    # unknown job refused
    with pytest.raises(InputValidationError):
        s.persist_from_processing("ghost", JobState.BLOCKED, updated_at=TS)
