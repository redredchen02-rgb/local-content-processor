from lcp.adapters.storage.audit_log import EVENT_ERASURE, AuditLog
from lcp.adapters.storage.job_store import BestEffortDeletionResult, JobStore
from lcp.core.state import JobState

TS = "2026-06-16T00:00:00Z"


def _setup(tmp_path):
    store = JobStore(base_dir=tmp_path)
    store.create_job("j1", created_at=TS)
    # put a blob in the job dir
    blob = store.job_dir("j1") / "raw" / "page.html"
    blob.write_text("scraped body with PII", encoding="utf-8")
    audit = AuditLog(store.job_dir("j1") / "audit.jsonl")
    return store, audit, blob


def test_delete_removes_files_best_effort(tmp_path):
    store, audit, blob = _setup(tmp_path)
    assert blob.exists()
    result = store.delete_job("j1", ts=TS, actor="operator", audit=audit)
    assert not store.job_dir("j1").exists()
    assert store.get_job("j1") is None
    assert result.removed is True


def test_delete_appends_erasure_event(tmp_path):
    store, audit, _ = _setup(tmp_path)
    # delete_job removes the dir (incl audit file); use an audit outside the dir
    outside_audit = AuditLog(tmp_path / "global_audit.jsonl")
    store.delete_job("j1", ts=TS, actor="operator", audit=outside_audit)
    lines = (tmp_path / "global_audit.jsonl").read_text().splitlines()
    import json

    last = json.loads(lines[-1])
    assert last["event"] == EVENT_ERASURE
    assert last["job_id"] == "j1"
    assert last["extra"]["cryptographic_erasure"] is False


def test_chain_still_verifies_after_erasure(tmp_path):
    store, _, _ = _setup(tmp_path)
    outside_audit = AuditLog(tmp_path / "global_audit.jsonl")
    outside_audit.append(ts=TS, stage="crawl", event="CRAWLED", job_id="j1", actor="m")
    store.delete_job("j1", ts=TS, actor="operator", audit=outside_audit)
    assert outside_audit.verify_chain() is True


def test_does_not_claim_cryptographic_erasure(tmp_path):
    store, audit, _ = _setup(tmp_path)
    result = store.delete_job("j1", ts=TS, actor="operator", audit=audit)
    # honest flag on the result type and the instance
    assert BestEffortDeletionResult.cryptographic_erasure is False
    assert result.cryptographic_erasure is False
    assert result.method == "best_effort_unlink"
    assert "cryptographic_erasure=False" in repr(result)
