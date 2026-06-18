"""Processor (adapter) tests: index load + I/O orchestration + state mapping.

These exercise the imperative shell (load index file, call pure rules, map to
JobState, write audit). Pure scoring is covered in tests/rules/."""

from __future__ import annotations

import json

import pytest

from lcp.adapters.processor import dedup_checker, risk_checker
from lcp.adapters.processor.dedup_checker import (
    SITE_INDEX_FILENAME,
    load_site_index,
    run_dedup_gate,
)
from lcp.adapters.processor.risk_checker import run_risk_gate
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.rules.dedup_rules import DedupReliability, DedupStatus
from lcp.core.rules.risk_rules import RiskInput, RiskStatus
from lcp.core.state import JobState, ReviewReason

TS = "2026-06-16T00:00:00Z"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _new_processing_job(store: JobStore, job_id: str) -> None:
    """Move a job to PROCESSING in-memory so a gate can persist a resting state.
    PROCESSING is transient (never written to SQLite) — we just walk the legal
    edges NEW->CRAWLED->(processing) by persisting CRAWLED, which is a legal
    predecessor of the gate target states via the PROCESSING edge."""
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)


def _write_index(path, *rows):
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


# --- index loading -----------------------------------------------------------


def test_load_missing_index_is_unavailable(tmp_path):
    idx = load_site_index(tmp_path / "nope.jsonl")
    assert idx.site_index_available is False
    assert idx.is_empty


def test_load_existing_empty_index_is_available(tmp_path):
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text("", encoding="utf-8")
    idx = load_site_index(p)
    assert idx.site_index_available is True
    assert idx.is_empty


def test_load_index_parses_entries(tmp_path):
    p = tmp_path / SITE_INDEX_FILENAME
    _write_index(
        p,
        {"job_id": "a", "title": "標題甲", "body": "內文甲"},
        {"job_id": "b", "title": "標題乙", "body": "內文乙"},
    )
    idx = load_site_index(p)
    assert idx.site_index_available is True
    assert {e.job_id for e in idx.entries} == {"a", "b"}


# --- U2: fail closed on a malformed index (per-line quarantine) ---------------


def test_load_index_quarantines_malformed_line_keeps_valid(tmp_path):
    """A stray non-JSON line must NOT raise out of the gate (process() only
    catches ExternalServiceError -> the job would be stuck CRAWLED at exit 5).
    Skip the bad line, keep the valid entries, stay available."""
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text(
        json.dumps({"job_id": "a", "title": "t", "body": "x"}, ensure_ascii=False)
        + "\n{ this is not json\n"
        + json.dumps({"job_id": "b", "title": "t2", "body": "y"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    idx = load_site_index(p)
    assert idx.site_index_available is True
    assert {e.job_id for e in idx.entries} == {"a", "b"}


def test_load_index_missing_job_id_is_skipped(tmp_path):
    """A well-formed JSON line missing job_id must not raise KeyError."""
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text(
        json.dumps({"title": "no id", "body": "x"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"job_id": "b", "title": "t", "body": "y"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    idx = load_site_index(p)
    assert idx.site_index_available is True
    assert {e.job_id for e in idx.entries} == {"b"}


def test_load_index_non_object_line_is_skipped(tmp_path):
    """A JSON scalar/array line (obj['job_id'] -> TypeError) must not raise."""
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text(
        "[1, 2, 3]\n42\n"
        + json.dumps({"job_id": "b", "title": "t", "body": "y"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    idx = load_site_index(p)
    assert {e.job_id for e in idx.entries} == {"b"}


def test_load_index_bom_prefixed_line_tolerated(tmp_path):
    """A leading UTF-8 BOM must not make an otherwise-valid line unparseable."""
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text(
        "﻿" + json.dumps({"job_id": "a", "title": "t", "body": "x"}),
        encoding="utf-8",
    )
    idx = load_site_index(p)
    assert {e.job_id for e in idx.entries} == {"a"}


def test_load_index_all_lines_unparseable_is_unavailable(tmp_path):
    """A non-empty file that yields ZERO valid entries is a misconfiguration —
    fail closed: mark unavailable so the pure layer downgrades unique->uncertain
    ->human review, rather than silently classifying everything UNIQUE."""
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text("garbage\n{nope\nstill not json\n", encoding="utf-8")
    idx = load_site_index(p)
    assert idx.site_index_available is False
    assert idx.is_empty


def test_dedup_gate_does_not_raise_on_corrupt_index(tmp_path, store, audit):
    """End-to-end through the gate: a corrupt index must yield an outcome, never
    raise a JSONDecodeError/KeyError past the fail-closed boundary."""
    p = tmp_path / SITE_INDEX_FILENAME
    p.write_text("{ broken\n", encoding="utf-8")
    _new_processing_job(store, "j1")
    out = run_dedup_gate(
        job_id="j1",
        title="某個標題",
        body="某段內文",
        store=store,
        audit=audit,
        site_index_path=p,
        ts=TS,
    )
    # All-unparseable -> unavailable -> downgrade to human review (fail closed).
    assert out.job_state is JobState.NEEDS_HUMAN_REVIEW
    assert out.review_reason is ReviewReason.DEDUP


# --- dedup gate orchestration -> state ---------------------------------------


def test_dedup_duplicate_maps_to_DUPLICATE_state(tmp_path, store, audit):
    p = tmp_path / SITE_INDEX_FILENAME
    _write_index(p, {"job_id": "old", "title": "重大事件 | ETtoday", "body": "x"})
    _new_processing_job(store, "j1")
    out = run_dedup_gate(
        job_id="j1",
        title="重大事件！",
        body="不同內文",
        store=store,
        audit=audit,
        ts=TS,
        site_index_path=p,
    )
    assert out.result.status == DedupStatus.DUPLICATE
    assert out.job_state == JobState.DUPLICATE
    assert store.get_job("j1").state == JobState.DUPLICATE


def test_dedup_uncertain_maps_to_review_with_dedup_reason(tmp_path, store, audit):
    # no site index file -> fail-loud LOW -> uncertain -> NEEDS_HUMAN_REVIEW
    _new_processing_job(store, "j2")
    out = run_dedup_gate(
        job_id="j2",
        title="某標題",
        body="某內文",
        store=store,
        audit=audit,
        ts=TS,
        site_index_path=tmp_path / "absent.jsonl",
    )
    assert out.result.status == DedupStatus.UNCERTAIN
    assert out.result.reliability == DedupReliability.LOW
    assert out.job_state == JobState.NEEDS_HUMAN_REVIEW
    assert out.review_reason == ReviewReason.DEDUP
    rec = store.get_job("j2")
    assert rec.state == JobState.NEEDS_HUMAN_REVIEW
    assert rec.review_reason == ReviewReason.DEDUP


def test_dedup_unique_does_not_write_state(tmp_path, store, audit):
    p = tmp_path / SITE_INDEX_FILENAME
    _write_index(p, {"job_id": "old", "title": "貓咪展覽", "body": "可愛貓咪"})
    _new_processing_job(store, "j3")
    out = run_dedup_gate(
        job_id="j3",
        title="股市收紅",
        body="台股大漲三百點 成交量放大",
        store=store,
        audit=audit,
        ts=TS,
        site_index_path=p,
    )
    assert out.result.status == DedupStatus.UNIQUE
    assert out.job_state is None
    # state untouched (caller continues the pipeline)
    assert store.get_job("j3").state == JobState.CRAWLED


def test_dedup_gate_writes_audit_with_reliability(tmp_path, store, audit):
    _new_processing_job(store, "j4")
    run_dedup_gate(
        job_id="j4",
        title="t",
        body="b",
        store=store,
        audit=audit,
        ts=TS,
        site_index_path=tmp_path / "absent.jsonl",
    )
    lines = audit._read_lines()
    gate = [l for l in lines if l["event"] == dedup_checker.EVENT_DEDUP_GATE]
    assert gate
    assert gate[-1]["extra"]["reliability"] == "low"
    assert audit.verify_chain()


def test_dedup_default_index_path_uses_store_base_dir(store, audit):
    # No site_index_path passed -> uses store.base_dir/site_index.jsonl, which
    # does not exist -> fail-loud uncertain (never crashes, never auto-rejects).
    _new_processing_job(store, "j5")
    out = run_dedup_gate(
        job_id="j5", title="t", body="b", store=store, audit=audit, ts=TS
    )
    assert out.result.reliability == DedupReliability.LOW
    assert out.result.status != DedupStatus.DUPLICATE  # never auto-reject


# --- risk gate orchestration -> state ----------------------------------------


def test_risk_redline_maps_to_BLOCKED(store, audit):
    _new_processing_job(store, "r1")
    out = run_risk_gate(
        job_id="r1",
        content=RiskInput(title="未成年外流", body="x", has_source=True),
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.result.status == RiskStatus.BLOCKED
    assert out.job_state == JobState.BLOCKED
    assert store.get_job("r1").state == JobState.BLOCKED


def test_risk_uncertain_maps_to_review_with_risk_reason(store, audit):
    class _Down:
        def detect(self, content):
            return [], False

    _new_processing_job(store, "r2")
    out = run_risk_gate(
        job_id="r2",
        content=RiskInput(title="x", body="y"),
        store=store,
        audit=audit,
        ts=TS,
        detector=_Down(),
    )
    assert out.job_state == JobState.NEEDS_HUMAN_REVIEW
    assert out.review_reason == ReviewReason.RISK
    assert store.get_job("r2").review_reason == ReviewReason.RISK


def test_risk_pass_does_not_write_state(store, audit):
    _new_processing_job(store, "r3")
    out = run_risk_gate(
        job_id="r3",
        content=RiskInput(title="美食市集", body="週末登場", has_source=True),
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.result.status == RiskStatus.PASS
    assert out.job_state is None
    assert store.get_job("r3").state == JobState.CRAWLED


def test_risk_gate_audit_is_pii_free_and_chained(store, audit):
    _new_processing_job(store, "r4")
    run_risk_gate(
        job_id="r4",
        content=RiskInput(title="某高中校園", body="學生", has_source=True),
        store=store,
        audit=audit,
        ts=TS,
    )
    lines = audit._read_lines()
    gate = [l for l in lines if l["event"] == risk_checker.EVENT_RISK_GATE][-1]
    # category codes only, never raw title/body
    assert "campus_student" in gate["extra"]["flag_categories"]
    assert "title" not in gate["extra"] and "body" not in gate["extra"]
    assert audit.verify_chain()


# --- Integration: three review sources carry distinct ReviewReasons ----------


def test_three_review_reasons_are_distinct_for_bucketing(store, audit):
    """risk / dedup / grounding each carry a distinct ReviewReason so per-gate
    accuracy can be bucketed (Success Criteria). grounding is Unit 7b's reason;
    here we assert the enum supports all three distinctly."""
    # risk gate -> RISK
    class _Down:
        def detect(self, content):
            return [], False

    _new_processing_job(store, "g1")
    risk_out = run_risk_gate(
        job_id="g1",
        content=RiskInput(),
        store=store,
        audit=audit,
        ts=TS,
        detector=_Down(),
    )
    # dedup gate -> DEDUP
    _new_processing_job(store, "g2")
    dedup_out = run_dedup_gate(
        job_id="g2",
        title="t",
        body="b",
        store=store,
        audit=audit,
        ts=TS,
        site_index_path="/does/not/exist.jsonl",
    )
    reasons = {risk_out.review_reason, dedup_out.review_reason, ReviewReason.GROUNDING}
    assert reasons == {ReviewReason.RISK, ReviewReason.DEDUP, ReviewReason.GROUNDING}
    assert len(reasons) == 3  # all distinct -> bucketable
