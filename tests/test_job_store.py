import json
import os
import shutil
import sqlite3
import threading

import pytest

from lcp.adapters.storage.audit_log import EVENT_ERASURE, AuditLog
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


def test_db_file_is_0600_independent_of_umask(tmp_path):
    # U18: lcp.db must be 0600 even if apply_hardening() (the umask) were ever
    # skipped — defense-in-depth on the store that shares lcp.db with the
    # plaintext-PII saved_sources table. Set a LOOSE umask first so the explicit
    # chmod, not the umask, is what produces 0600.
    old = os.umask(0o000)
    try:
        s = _store(tmp_path)
    finally:
        os.umask(old)
    mode = os.stat(s.db_path).st_mode & 0o777
    assert mode == 0o600, oct(mode)


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


def test_persist_from_processing_commit_survives_marker_clear_failure(tmp_path, monkeypatch):
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
    rec = s.set_state("j1", JobState.CRAWLED_WARN, updated_at=TS, review_reason=ReviewReason.DEDUP)
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
        c1.execute("UPDATE jobs SET error_code = ? WHERE job_id = ?", ("E1", "j1"))
        c1.commit()
        row = c2.execute("SELECT error_code FROM jobs WHERE job_id = ?", ("j1",)).fetchone()
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
    rec = s.persist_from_processing(
        "j", JobState.BLOCKED, updated_at=TS, review_reason=ReviewReason.RISK
    )
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
        s,
        "clear_processing",
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


# --- U9: persist_crawl_result — state + hashes in ONE transaction ------------


def test_persist_crawl_result_lands_state_and_hashes_atomically(tmp_path):
    """The combined method writes the transition AND the source hashes together:
    after it, BOTH are present (the torn write the two-call sequence allowed is
    gone)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)  # NEW
    rec = s.persist_crawl_result(
        "j1",
        JobState.CRAWLED,
        updated_at=TS,
        source_html_sha256="h" * 64,
        source_text_sha256="t" * 64,
    )
    assert rec.state is JobState.CRAWLED
    assert rec.source_html_sha256 == "h" * 64
    assert rec.source_text_sha256 == "t" * 64
    # Re-read from SQLite: state and hashes landed together, not just on the
    # returned record.
    got = s.get_job("j1")
    assert got.state is JobState.CRAWLED
    assert got.source_html_sha256 == "h" * 64
    assert got.source_text_sha256 == "t" * 64


def test_persist_crawl_result_illegal_transition_writes_nothing(tmp_path):
    """An illegal predecessor refuses BEFORE any mutation — neither the state nor
    the hashes are partially written (the old two-transaction sequence committed
    the hashes before the state transition validated, a partial mutation)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    s.set_state("j1", JobState.CRAWLED, updated_at=TS)  # already crawled
    # CRAWLED -> CRAWLED is not a legal edge -> refuse.
    with pytest.raises(InputValidationError):
        s.persist_crawl_result(
            "j1",
            JobState.CRAWLED,
            updated_at=TS,
            source_html_sha256="h" * 64,
            source_text_sha256="t" * 64,
        )
    got = s.get_job("j1")
    assert got.state is JobState.CRAWLED
    # The refused call must not have stamped the new hashes (no partial mutation).
    assert got.source_html_sha256 is None
    assert got.source_text_sha256 is None


def test_persist_crawl_result_unknown_job_refused(tmp_path):
    with pytest.raises(InputValidationError):
        _store(tmp_path).persist_crawl_result("ghost", JobState.CRAWLED, updated_at=TS)


def test_persist_crawl_result_refuses_transient_target(tmp_path):
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    with pytest.raises(InputValidationError):
        s.persist_crawl_result("j1", JobState.PROCESSING, updated_at=TS)


def test_persist_crawl_result_uses_single_connection(tmp_path, monkeypatch):
    """One transaction == one _connect (read+update folded), so a crash can never
    interleave between two separate committed writes."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    n = {"calls": 0}
    orig = s._connect

    def counting():
        n["calls"] += 1
        return orig()

    monkeypatch.setattr(s, "_connect", counting)
    s.persist_crawl_result("j1", JobState.CRAWLED, updated_at=TS, source_text_sha256="t" * 64)
    assert n["calls"] == 1


def test_persist_crawl_result_concurrent_one_winner(tmp_path):
    """BEGIN IMMEDIATE closes the read->update race: two crawls racing the same
    NEW job to different outcomes resolve to exactly one winner; the loser reads
    the committed state and refuses the now-illegal transition."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def worker(name, target):
        barrier.wait()
        try:
            s.persist_crawl_result("j1", target, updated_at=TS, source_text_sha256="t" * 64)
            results[name] = "ok"
        except InputValidationError:
            results[name] = "illegal"
        except Exception as e:  # noqa: BLE001 - surface SQLITE_BUSY etc. as failure
            results[name] = f"other:{type(e).__name__}"

    # CRAWLED vs CRAWL_FAILED: from NEW both are legal, but whichever lands first
    # has NO legal edge to the other (CRAWLED->{CRAWLED_WARN,PROCESSING};
    # CRAWL_FAILED->{NEW}), so the loser always refuses regardless of order.
    t1 = threading.Thread(target=worker, args=("crawled", JobState.CRAWLED))
    t2 = threading.Thread(target=worker, args=("failed", JobState.CRAWL_FAILED))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [k for k, v in results.items() if v == "ok"]
    illegals = [k for k, v in results.items() if v == "illegal"]
    assert len(oks) == 1, results
    assert len(illegals) == 1, results
    # The winner's hashes landed atomically with its state.
    got = s.get_job("j1")
    assert got.state in (JobState.CRAWLED, JobState.CRAWL_FAILED)
    assert got.source_text_sha256 == "t" * 64


# --- U10: truthful delete/erasure + BEGIN IMMEDIATE on the delete-row write ---


def _erasure_events(audit_path):
    """All ERASURE events in file order (external audit log)."""
    return [
        e
        for e in (
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if e["event"] == EVENT_ERASURE
    ]


def test_delete_records_truthful_outcome_on_clean_removal(tmp_path):
    """A normal delete removes blobs + row AND records a confirming ERASURE event
    whose extra reflects the REAL outcome (removed=True, the row was deleted)."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    (s.job_dir("j1") / "raw" / "page.html").write_text("body", encoding="utf-8")
    # External audit (storage-root layout) so BOTH events survive the rmtree.
    audit = AuditLog(tmp_path / "audit.jsonl")

    result = s.delete_job("j1", ts=TS, actor="operator", audit=audit)

    assert result.removed is True
    assert not s.job_dir("j1").exists()
    assert s.get_job("j1") is None
    events = _erasure_events(tmp_path / "audit.jsonl")
    # A confirming event records the TRUE outcome (not just the pre-rmtree intent).
    confirm = events[-1]
    assert confirm["extra"]["removed"] is True
    assert confirm["extra"]["rows_deleted"] == 1
    assert confirm["extra"]["cryptographic_erasure"] is False


def test_delete_reports_not_fully_removed_when_rmtree_fails(tmp_path, monkeypatch):
    """The compliance bug U10 closes: a stuck/undeletable file must NOT be reported
    as erased. With rmtree failing (dir survives), the returned result AND the
    truthful audit event both say removed=False — never an unconditional 'erased'
    paired with removed=False."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    (s.job_dir("j1") / "raw" / "stuck.bin").write_text("held", encoding="utf-8")
    audit = AuditLog(tmp_path / "audit.jsonl")

    def boom(path, *a, **k):  # rmtree raises (e.g. permission-denied / held file)
        raise OSError("device busy")

    monkeypatch.setattr(shutil, "rmtree", boom)

    result = s.delete_job("j1", ts=TS, actor="operator", audit=audit)

    assert result.removed is False  # truthful: the blob dir still exists
    assert s.job_dir("j1").exists()
    confirm = _erasure_events(tmp_path / "audit.jsonl")[-1]
    assert confirm["extra"]["removed"] is False  # audit matches reality, no mismatch


def test_delete_unknown_job_no_spurious_audit_or_crash(tmp_path):
    """Deleting an unknown id behaves predictably: no crash, removed reflects that
    nothing was there, rows_deleted=0, and the truthful event says so."""
    s = _store(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")

    result = s.delete_job("ghost", ts=TS, actor="operator", audit=audit)

    assert result.removed is False  # nothing to remove
    confirm = _erasure_events(tmp_path / "audit.jsonl")[-1]
    assert confirm["extra"]["rows_deleted"] == 0
    assert confirm["extra"]["dir_existed"] is False


def test_delete_uses_begin_immediate_single_connection(tmp_path, monkeypatch):
    """The delete-row write holds the WAL write lock under BEGIN IMMEDIATE on a
    single connection (uniform with set_state/persist_crawl_result). A trace
    callback observes every SQL statement issued on the connection."""
    s = _store(tmp_path)
    s.create_job("j1", created_at=TS)
    seen: list[str] = []
    connects = {"n": 0}
    orig_connect = s._connect

    def tracing_connect():
        connects["n"] += 1
        conn = orig_connect()
        conn.set_trace_callback(lambda sql: seen.append(sql.strip().split()[0].upper()))
        return conn

    monkeypatch.setattr(s, "_connect", tracing_connect)
    s.delete_job("j1", ts=TS, actor="operator", audit=None)
    assert connects["n"] == 1  # one connection for the row delete
    assert "BEGIN" in seen  # explicit BEGIN IMMEDIATE, not the legacy implicit BEGIN
    assert "DELETE" in seen


def test_delete_row_concurrent_writers_no_busy(tmp_path):
    """N concurrent deletes of distinct rows serialize under the write lock and all
    complete without SQLITE_BUSY (the BEGIN IMMEDIATE hold must not regress into
    contention), mirroring the existing N-writer set_state test."""
    s = _store(tmp_path)
    n = 8
    for i in range(n):
        s.create_job(f"j{i}", created_at=TS)
    barrier = threading.Barrier(n)
    errors: list[str] = []

    def worker(i):
        barrier.wait()
        try:
            s.delete_job(f"j{i}", ts=TS, actor="operator", audit=None)
        except Exception as e:  # noqa: BLE001 - any raise is a regression here
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert all(s.get_job(f"j{i}") is None for i in range(n))


def test_bump_interrupt_count_concurrent_no_lost_increments(tmp_path):
    """F5: two concurrent bump_interrupt_count() calls on the same job must each
    see distinct return values (1 and 2). A non-serialized read-modify-write loses
    one increment — both threads read 0, both write 1, final value = 1 instead of 2."""
    s = _store(tmp_path)
    s.create_job("jc", created_at=TS)
    n = 8
    barrier = threading.Barrier(n)
    results: list[int] = []
    errors: list[str] = []

    def worker():
        barrier.wait()
        try:
            v = s.bump_interrupt_count("jc")
            results.append(v)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert sorted(results) == list(range(1, n + 1)), (
        f"expected increments 1..{n}, got {sorted(results)} — concurrent bumps lost increments"
    )
