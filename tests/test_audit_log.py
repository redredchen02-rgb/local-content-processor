import hashlib
import json
import os
import threading

import pytest

from lcp.adapters.storage.audit_log import EVENT_ERASURE, AuditLog
from lcp.core.errors import DependencyError, InputValidationError

TS = "2026-06-16T00:00:00Z"


def _log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _append_n(log, n):
    for i in range(n):
        log.append(ts=TS, stage="crawl", event=f"E{i}", job_id="j1", actor="machine")


def test_append_and_verify_chain(tmp_path):
    log = _log(tmp_path)
    _append_n(log, 5)
    assert log.verify_chain() is True
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 5
    # seq is monotonic
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == [0, 1, 2, 3, 4]


def test_tamper_middle_line_detected(tmp_path):
    log = _log(tmp_path)
    _append_n(log, 5)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["event"] = "TAMPERED"  # edit payload but keep old hash
    lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    assert log.verify_chain() is False


def test_artifact_sha256_recorded(tmp_path):
    log = _log(tmp_path)
    h = hashlib.sha256(b"draft-content").hexdigest()
    rec = log.append(
        ts=TS,
        stage="assemble",
        event="DRAFT",
        job_id="j1",
        actor="machine",
        artifact_sha256=h,
    )
    assert rec["artifact_sha256"] == h
    assert log.verify_chain()


def test_pii_keys_rejected(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(InputValidationError):
        log.append(
            ts=TS,
            stage="crawl",
            event="X",
            job_id="j1",
            actor="m",
            extra={"title": "leaked headline"},
        )
    with pytest.raises(InputValidationError):
        log.append(
            ts=TS,
            stage="crawl",
            event="X",
            job_id="j1",
            actor="m",
            extra={"source_url": "https://x.com/u"},
        )


def test_bad_artifact_hash_rejected(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(InputValidationError):
        log.append(
            ts=TS,
            stage="x",
            event="X",
            job_id="j1",
            actor="m",
            artifact_sha256="not-a-hash",
        )


def test_erasure_event_recorded(tmp_path):
    log = _log(tmp_path)
    log.append(ts=TS, stage="storage", event=EVENT_ERASURE, job_id="j1", actor="op")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["event"] == EVENT_ERASURE
    assert log.verify_chain()


def test_concurrent_appends_keep_seq_unique_and_chain_valid(tmp_path):
    # P0 regression: the GUI runs gates in background threads, so multiple
    # threads append to the SAME AuditLog concurrently. Without an exclusive
    # lock held across read-tail + write, two appends read the same tail and
    # commit a duplicate seq, corrupting the hash chain. With the flock fix,
    # seqs stay unique/contiguous and verify_chain() passes.
    log = _log(tmp_path)
    n = 40
    barrier = threading.Barrier(n)

    def _worker(i):
        barrier.wait()  # maximise contention on the read-tail/write window
        log.append(ts=TS, stage="crawl", event=f"E{i}", job_id="j1", actor="machine")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == n
    seqs = sorted(json.loads(line)["seq"] for line in lines)
    assert seqs == list(range(n))  # unique + contiguous, no duplicates
    assert log.verify_chain() is True


def test_append_fails_loud_without_fcntl(tmp_path, monkeypatch):
    # U19: on a non-POSIX host fcntl imports as None, which silently turns the
    # flock(LOCK_EX) read-tail+write serialization into a NO-OP — concurrent
    # GUI background-thread appends could then corrupt the hash chain with no
    # signal. The audit log is the tamper-evidence backbone, so append() must
    # REFUSE LOUD (DependencyError) rather than append lock-free.
    import lcp.adapters.storage.audit_log as audit_mod

    monkeypatch.setattr(audit_mod, "fcntl", None)
    log = _log(tmp_path)
    with pytest.raises(DependencyError):
        log.append(ts=TS, stage="crawl", event="E0", job_id="j1", actor="m")
    # Nothing was written: refusing loud must not leave a lock-free tail line.
    assert not (tmp_path / "audit.jsonl").exists() or (tmp_path / "audit.jsonl").read_text() == ""


def test_append_fsyncs_parent_dir(tmp_path, monkeypatch):
    # U18: after writing+fsyncing the line, append() must also fsync the PARENT
    # DIRECTORY fd so a crash cannot lose the freshly-appended (otherwise
    # fsynced) tail line — a lost tail would make verify_chain() falsely report
    # tampering on a merely-truncated log. Assert the dir fd is fsynced on the
    # FIRST append (which also creates the dir entry) and that append+verify
    # still work end-to-end.
    import lcp.adapters.storage.audit_log as audit_mod

    fsynced_fds: list[int] = []
    real_fsync = os.fsync

    def _tracking_fsync(fd):
        fsynced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(audit_mod.os, "fsync", _tracking_fsync)

    log = _log(tmp_path)
    rec = log.append(ts=TS, stage="crawl", event="E0", job_id="j1", actor="m")

    # The file fd plus the directory fd were both fsynced (>=2 distinct fds).
    assert len(fsynced_fds) >= 2
    # Behavioral: the append landed and the chain verifies.
    assert rec["seq"] == 0
    assert log.verify_chain() is True

    # A second append (dir entry already exists) still fsyncs and verifies.
    fsynced_fds.clear()
    log.append(ts=TS, stage="crawl", event="E1", job_id="j1", actor="m")
    assert len(fsynced_fds) >= 2
    assert log.verify_chain() is True


def test_chain_links_prev_hash(tmp_path):
    log = _log(tmp_path)
    _append_n(log, 3)
    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert lines[0]["prev_hash"] == "0" * 64
    assert lines[1]["prev_hash"] == lines[0]["hash"]
    assert lines[2]["prev_hash"] == lines[1]["hash"]


def test_last_line_bounded_read_edge_cases(tmp_path):
    """Batch-2 perf: _tail reads only the last line via a bounded backward read.
    Cover the tricky inputs that backward-reading must tolerate."""
    log = _log(tmp_path)
    p = tmp_path / "audit.jsonl"
    assert log._last_line() is None  # nonexistent
    p.write_text('{"seq":0,"hash":"a"}', encoding="utf-8")  # no trailing newline
    assert log._last_line() == '{"seq":0,"hash":"a"}'
    p.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert log._last_line() == "three"
    p.write_text("one\ntwo\n\n\n", encoding="utf-8")  # trailing blank lines
    assert log._last_line() == "two"
    p.write_text("\n\n", encoding="utf-8")  # whitespace-only
    assert log._last_line() is None


def test_tail_handles_record_larger_than_block(tmp_path):
    """A single record bigger than the 4 KiB read block must still be tailed
    correctly (multi-block backward read), and the chain must keep going."""
    log = _log(tmp_path)
    big = "x" * 9000  # > 4096 -> forces the backward read to expand
    log.append(ts=TS, stage="crawl", event="E0", job_id="j1", actor="m", extra={"pad": big})
    log.append(ts=TS, stage="crawl", event="E1", job_id="j1", actor="m")
    assert log.verify_chain() is True
    seq, _ = log._tail()
    assert seq == 2
