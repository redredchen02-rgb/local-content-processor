"""Unit 7: de-watermark segregation-of-duties attestation."""

from __future__ import annotations

import json

import pytest

from lcp.adapters.publisher import dewatermark as dw
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.errors import InputValidationError

TS = "2026-06-17T00:00:00Z"


def _ctx(tmp_path, reviewers=("alice", "bob")):
    base = str(tmp_path)
    store = JobStore(base_dir=base)
    store.create_job("j1", created_at=TS)
    audit = AuditLog(tmp_path / "audit.jsonl")
    config = Config(publisher=PublisherConfig(reviewers=list(reviewers)))
    return store, audit, config


def test_attest_requires_recorded_submitter(tmp_path):
    store, audit, config = _ctx(tmp_path)
    with pytest.raises(InputValidationError):
        dw.attest_dewatermark("j1", "alice", "contract-42", config=config, store=store, audit=audit, ts=TS)


def test_full_attestation_unlocks(tmp_path):
    store, audit, config = _ctx(tmp_path)
    dw.request_dewatermark("j1", "bob", store=store, audit=audit, ts=TS)
    att = dw.attest_dewatermark("j1", "alice", "contract-42", config=config, store=store, audit=audit, ts=TS)
    assert att.attested
    assert att.submitter == "bob" and att.reviewer == "alice"
    assert len(att.evidence_sha256) == 64
    assert dw.read_attestation(store, "j1") is not None


def test_reviewer_equals_submitter_rejected(tmp_path):
    store, audit, config = _ctx(tmp_path)
    dw.request_dewatermark("j1", "alice", store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError, match="segregation of duties"):
        dw.attest_dewatermark("j1", "alice", "contract-42", config=config, store=store, audit=audit, ts=TS)
    assert dw.read_attestation(store, "j1") is None  # stays locked


def test_missing_evidence_rejected(tmp_path):
    store, audit, config = _ctx(tmp_path)
    dw.request_dewatermark("j1", "bob", store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError):
        dw.attest_dewatermark("j1", "alice", "   ", config=config, store=store, audit=audit, ts=TS)
    assert dw.read_attestation(store, "j1") is None


def test_non_whitelisted_reviewer_rejected(tmp_path):
    store, audit, config = _ctx(tmp_path)
    dw.request_dewatermark("j1", "bob", store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError):
        dw.attest_dewatermark("j1", "mallory", "contract-42", config=config, store=store, audit=audit, ts=TS)


def test_default_locked_no_attestation(tmp_path):
    store, _, _ = _ctx(tmp_path)
    assert dw.read_attestation(store, "j1") is None


def test_audit_is_pii_free_evidence_hashed_not_raw(tmp_path):
    store, audit, config = _ctx(tmp_path)
    dw.request_dewatermark("j1", "bob", store=store, audit=audit, ts=TS)
    dw.attest_dewatermark(
        "j1", "alice", "https://licenses.example/secret-token-xyz",
        config=config, store=store, audit=audit, ts=TS,
    )
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    # raw evidence never enters the audit; only its hash
    assert "secret-token-xyz" not in text
    assert "DEWATERMARK_ATTESTED" in text
    # raw evidence IS recoverable from the 0600 operator file
    evid = (store.job_dir("j1") / "review" / "dewatermark_evidence.txt").read_text(encoding="utf-8")
    assert "secret-token-xyz" in evid


def test_disclaimer_verbatim(tmp_path):
    store, audit, config = _ctx(tmp_path)
    dw.request_dewatermark("j1", "bob", store=store, audit=audit, ts=TS)
    dw.attest_dewatermark("j1", "alice", "c-1", config=config, store=store, audit=audit, ts=TS)
    att_file = json.loads((store.job_dir("j1") / "review" / "dewatermark_attestation.json").read_text(encoding="utf-8"))
    assert att_file["disclaimer"] == dw.DEWATERMARK_DISCLAIMER
    assert "ATTESTATION, NOT AUTHENTICATION" in dw.DEWATERMARK_DISCLAIMER
