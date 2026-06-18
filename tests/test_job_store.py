import os
import sqlite3
import threading

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


# --- U7: per-job-dir crash-attempt counter (no jobs-schema column) -----------


def test_interrupt_count_defaults_to_zero(tmp_path):
    """A job with no .interrupt_count file reads as 0 (never crashes on absence)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    assert s.read_interrupt_count("j1") == 0


def test_interrupt_count_increments_and_persists(tmp_path):
    """bump_interrupt_count returns the new value and persists it across calls."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    assert s.bump_interrupt_count("j1") == 1
    assert s.read_interrupt_count("j1") == 1
    assert s.bump_interrupt_count("j1") == 2
    assert s.read_interrupt_count("j1") == 2


def test_interrupt_count_cleared(tmp_path):
    """clear_interrupt_count resets the counter (a clean re-process starts fresh)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.bump_interrupt_count("j1")
    s.clear_interrupt_count("j1")
    assert s.read_interrupt_count("j1") == 0


def test_interrupt_count_file_is_0600(tmp_path):
    """The counter is a per-job-dir file (no SQLite column) written 0600 — it must
    never carry PII and must not be world-readable (PII-at-rest discipline)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.bump_interrupt_count("j1")
    counter = s.job_dir("j1") / ".interrupt_count"
    assert counter.exists()
    assert (counter.stat().st_mode & 0o777) == 0o600


def test_interrupt_count_tolerates_corrupt_file(tmp_path):
    """A garbage/half-written counter file reads as 0 (fail-safe), never raises."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    (s.job_dir("j1") / ".interrupt_count").write_text("not-a-number", encoding="utf-8")
    assert s.read_interrupt_count("j1") == 0
    # And a bump still recovers to a clean count.
    assert s.bump_interrupt_count("j1") == 1


# --- U7 / plan-004 deferred: clear-after-COMMIT failure must not corrupt row ---


def test_persist_from_processing_commit_survives_marker_clear_failure(
    tmp_path, monkeypatch
):
    """plan-004 deferred 'marker-touch-failure rollback' test.

    persist_from_processing commits the resting state FIRST, then clears the marker
    OUTSIDE the transaction. If clear_processing() fails (e.g. EROFS / lock), the
    committed resting state must still stand — the row is already durable — and the
    stale marker is exactly what U7's reconciliation later cleans up. The DB must
    never roll back a committed state because of a post-commit filesystem error."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    s.mark_processing("j1")

    def boom(_job_id):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(s, "clear_processing", boom)
    with pytest.raises(OSError):
        s.persist_from_processing("j1", JobState.BLOCKED, updated_at=TS)
    # The resting state committed before the marker clear — it must persist.
    assert s.get_job("j1").state is JobState.BLOCKED


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


# --- U1: set_state single-connection + BEGIN IMMEDIATE -----------------------


def _at_review_pending(s, jid="j1"):
    """Drive a job to REVIEW_PENDING (the only path runs through PROCESSING)."""
    s.create_job(jid, created_at=TS)
    s.set_state(jid, JobState.CRAWLED, updated_at=TS)
    s.persist_from_processing(jid, JobState.PROCESSED, updated_at=TS)
    s.set_state(jid, JobState.REVIEW_PENDING, updated_at=TS)


def test_set_state_uses_single_connection(tmp_path, monkeypatch):
    """The fold removes the redundant read connection: one _connect per call
    (was two — get_job's connection + the UPDATE connection)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    n = {"calls": 0}
    orig = s._connect

    def counting():
        n["calls"] += 1
        return orig()

    monkeypatch.setattr(s, "_connect", counting)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    assert n["calls"] == 1


def test_set_state_does_not_touch_processing_marker(tmp_path, monkeypatch):
    """set_state is a general transition: it must NOT inherit
    persist_from_processing's marker handling (mark/clear)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    seen = {"mark": 0, "clear": 0}
    monkeypatch.setattr(
        s, "mark_processing", lambda *a, **k: seen.__setitem__("mark", seen["mark"] + 1)
    )
    monkeypatch.setattr(
        s, "clear_processing",
        lambda *a, **k: seen.__setitem__("clear", seen["clear"] + 1),
    )
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    assert seen == {"mark": 0, "clear": 0}


def test_set_state_concurrent_same_row_one_winner(tmp_path):
    """BEGIN IMMEDIATE closes the read->update race: two writers racing the same
    REVIEW_PENDING row (-> APPROVED vs -> REJECTED) resolve to exactly one
    winner; the loser reads the committed state and refuses the now-illegal
    transition (no double transition)."""
    s = _store(tmp_path)
    _at_review_pending(s, "j1")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def worker(name, target):
        barrier.wait()
        try:
            s.set_state("j1", target, updated_at=TS)
            results[name] = "ok"
        except InputValidationError:
            results[name] = "illegal"
        except Exception as e:  # noqa: BLE001 - surface SQLITE_BUSY etc. as failure
            results[name] = f"other:{type(e).__name__}"

    t1 = threading.Thread(target=worker, args=("approve", JobState.APPROVED))
    t2 = threading.Thread(target=worker, args=("reject", JobState.REJECTED))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [k for k, v in results.items() if v == "ok"]
    illegals = [k for k, v in results.items() if v == "illegal"]
    assert len(oks) == 1, results
    assert len(illegals) == 1, results
    expected = JobState.APPROVED if oks[0] == "approve" else JobState.REJECTED
    assert s.get_job("j1").state is expected


def test_set_state_many_concurrent_writers_no_busy(tmp_path):
    """The trade BEGIN IMMEDIATE makes (longer write-lock hold) must not regress
    into SQLITE_BUSY: N writers on distinct rows serialize under busy_timeout and
    all complete without raising."""
    s = _store(tmp_path)
    n = 8
    for i in range(n):
        s.create_job(f"j{i}", created_at=TS)
    barrier = threading.Barrier(n)
    errors: list[str] = []

    def worker(i):
        barrier.wait()
        try:
            s.set_state(f"j{i}", JobState.CRAWLED, updated_at=TS)
        except Exception as e:  # noqa: BLE001 - any raise is a regression here
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert all(s.get_job(f"j{i}").state is JobState.CRAWLED for i in range(n))


def test_persist_from_processing_concurrent_one_winner(tmp_path):
    """BEGIN IMMEDIATE closes the read->update race in persist_from_processing too:
    two gates racing the same CRAWLED job to different terminal targets resolve to
    exactly one winner; the loser reads the committed terminal state and refuses
    (PROCESSING is unreachable from a terminal state). No marker is leaked."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def worker(name, target):
        barrier.wait()
        try:
            s.persist_from_processing("j1", target, updated_at=TS)
            results[name] = "ok"
        except InputValidationError:
            results[name] = "illegal"
        except Exception as e:  # noqa: BLE001 - surface SQLITE_BUSY etc. as failure
            results[name] = f"other:{type(e).__name__}"

    t1 = threading.Thread(target=worker, args=("blocked", JobState.BLOCKED))
    t2 = threading.Thread(target=worker, args=("duplicate", JobState.DUPLICATE))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [k for k, v in results.items() if v == "ok"]
    illegals = [k for k, v in results.items() if v == "illegal"]
    assert len(oks) == 1, results
    assert len(illegals) == 1, results
    expected = JobState.BLOCKED if oks[0] == "blocked" else JobState.DUPLICATE
    assert s.get_job("j1").state is expected
    assert not s.is_processing("j1")  # marker cleared by the winner, none leaked
