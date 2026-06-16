import hashlib
import json

import pytest

from lcp.adapters.storage.audit_log import EVENT_ERASURE, AuditLog
from lcp.core.errors import InputValidationError

TS = "2026-06-16T00:00:00Z"


def _log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _append_n(log, n):
    for i in range(n):
        log.append(
            ts=TS, stage="crawl", event=f"E{i}", job_id="j1", actor="machine"
        )


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
        ts=TS, stage="assemble", event="DRAFT", job_id="j1",
        actor="machine", artifact_sha256=h,
    )
    assert rec["artifact_sha256"] == h
    assert log.verify_chain()


def test_pii_keys_rejected(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(InputValidationError):
        log.append(
            ts=TS, stage="crawl", event="X", job_id="j1", actor="m",
            extra={"title": "leaked headline"},
        )
    with pytest.raises(InputValidationError):
        log.append(
            ts=TS, stage="crawl", event="X", job_id="j1", actor="m",
            extra={"source_url": "https://x.com/u"},
        )


def test_bad_artifact_hash_rejected(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(InputValidationError):
        log.append(
            ts=TS, stage="x", event="X", job_id="j1", actor="m",
            artifact_sha256="not-a-hash",
        )


def test_erasure_event_recorded(tmp_path):
    log = _log(tmp_path)
    log.append(ts=TS, stage="storage", event=EVENT_ERASURE, job_id="j1", actor="op")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["event"] == EVENT_ERASURE
    assert log.verify_chain()


def test_chain_links_prev_hash(tmp_path):
    log = _log(tmp_path)
    _append_n(log, 3)
    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert lines[0]["prev_hash"] == "0" * 64
    assert lines[1]["prev_hash"] == lines[0]["hash"]
    assert lines[2]["prev_hash"] == lines[1]["hash"]
