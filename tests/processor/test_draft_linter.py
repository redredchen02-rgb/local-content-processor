"""Draft lint + grounding gate orchestration tests (imperative shell).

Exercise: load nothing from the network, run pure lint+grounding, map to
JobState via persist_gate_state (same seam as risk/dedup), write PII-free audit.
Pure scoring is covered in tests/rules/. Also pins: grounding fail ->
NEEDS_HUMAN_REVIEW(reason=grounding); after clearing, lint re-runs; and the gate
never resolves a URL.
"""

from __future__ import annotations

import socket

import pytest

from lcp.adapters.processor.draft_linter import (
    EVENT_GROUNDING_GATE,
    EVENT_LINT_GATE,
    build_lint_config,
    relint_after_grounding_cleared,
    relint_clears_hold,
    run_draft_lint_gate,
)
from lcp.adapters.processor.media_checker import media_presence
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import ContentConfig
from lcp.core.draft import Draft, FaqItem, MediaSection, SourceQuote
from lcp.core.rules.lint_rules import LintConfig
from lcp.core.state import JobState, ReviewReason

TS = "2026-06-16T00:00:00Z"

CATEGORIES = {"美食": [], "社會": []}

SOURCE = (
    "華山文創園區本週末舉辦美食市集。\n"
    "現場有上百個攤位提供各式小吃與飲料。\n"
    "主辦單位預估將吸引大量人潮前往參觀。"
)


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture()
def lint_config():
    # Set loose Unit-1 field constraints so existing tests (grounding, audit,
    # state-machine) are not disturbed by the new length/count rules.
    # Unit-1 constraint correctness is covered in tests/rules/test_lint_rules.py.
    return build_lint_config(
        ContentConfig(
            title_min_chars=8,
            title_max_chars=35,
            intro_min_chars=1,
            intro_max_chars=9999,
            event_body_min_chars=1,
            event_body_max_chars=9999,
            summary_warn_chars=9998,
            summary_error_chars=9999,
            faq_min_count=1,
            faq_max_count=99,
            quick_facts_min_count=1,
            quick_facts_max_count=99,
        ),
        CATEGORIES,
    )


def _new_processing_job(store: JobStore, job_id: str) -> None:
    """NEW -> CRAWLED (a legal PROCESSING-predecessor) so a gate can persist a
    resting state via the PROCESSING edge (same pattern as the dedup/risk tests)."""
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)


def _good_draft(**overrides) -> Draft:
    base = dict(
        title="台北華山美食市集週末熱鬧登場",
        intro="華山文創園區本週末舉辦美食市集。",
        quick_facts=["時間：週末", "地點：華山", "免費"],
        event_body="華山文創園區本週末舉辦美食市集。現場有上百個攤位提供各式小吃與飲料。",
        image_sections=[MediaSection(asset_ref="img/a.jpg", caption="攤位")],
        faq=[FaqItem(question="要錢嗎？", answer="現場有上百個攤位提供各式小吃與飲料")],
        # summary is now grounded (Unit 1 / D1: copywriter summary is checked) —
        # a verbatim source line keeps this "good" fixture passing grounding.
        summary="主辦單位預估將吸引大量人潮前往參觀。",
        tags=["美食", "市集", "華山"],
        keywords=["美食"],
        category="美食",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    base.update(overrides)
    return Draft(**base)


# --- relint applies media-conditional rules (D9 fail-open fix) ----------------


def test_relint_requires_image_sections_when_bundle_has_images(audit, lint_config):
    # An image-bearing job whose draft has no image_sections must NOT clear on
    # relint — otherwise the grounding-hold recovery path silently skips the
    # conditional image-section requirement (fail-open the correctness review found).
    d = _good_draft(image_sections=[])
    assert (
        relint_clears_hold(
            job_id="ri",
            draft=d,
            source_text=SOURCE,
            lint_config=lint_config,
            audit=audit,
            ts=TS,
            has_images=True,
        )
        is False
    )
    # Text-only (no bundle images) -> image_sections not required -> clears.
    assert (
        relint_clears_hold(
            job_id="ri2",
            draft=d,
            source_text=SOURCE,
            lint_config=lint_config,
            audit=audit,
            ts=TS,
            has_images=False,
        )
        is True
    )


def test_media_presence_reads_persisted_report(store):
    import json

    job_dir = store.job_dir("mp")
    (job_dir / "processed").mkdir(parents=True, exist_ok=True)
    (job_dir / "processed" / "validation_report.json").write_text(
        json.dumps({"image_count": 2, "video_count": 0}), encoding="utf-8"
    )
    assert media_presence(store, "mp") == (True, False)
    # Absent report -> conservative (False, False) floor (never happens at a
    # grounding hold, where media has already run).
    assert media_presence(store, "absent") == (False, False)


# --- Happy path: both pass -> no state write ---------------------------------


def test_clean_draft_passes_no_state_write(store, audit, lint_config):
    _new_processing_job(store, "j1")
    out = run_draft_lint_gate(
        job_id="j1",
        draft=_good_draft(),
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.grounding.passed
    assert out.lint.passed
    assert out.job_state is None
    # state untouched: caller continues the pipeline toward PROCESSED
    assert store.get_job("j1").state == JobState.CRAWLED


# --- grounding fail -> NEEDS_HUMAN_REVIEW(reason=grounding) -------------------


def test_grounding_fail_routes_to_human_review(store, audit, lint_config):
    _new_processing_job(store, "j2")
    bad = _good_draft(
        quotes=[SourceQuote(text="主辦單位收受廠商賄賂三百萬元")]  # not in source
    )
    out = run_draft_lint_gate(
        job_id="j2",
        draft=bad,
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.job_state == JobState.NEEDS_HUMAN_REVIEW
    assert out.review_reason == ReviewReason.GROUNDING
    assert out.lint is None  # lint not run when grounding fails first
    rec = store.get_job("j2")
    assert rec.state == JobState.NEEDS_HUMAN_REVIEW
    assert rec.review_reason == ReviewReason.GROUNDING


def test_unsupported_accusation_routes_to_grounding_review(store, audit, lint_config):
    _new_processing_job(store, "j2b")
    bad = _good_draft(
        event_body="市長疑似收受巨額賄賂並掩蓋醜聞遭到檢方約談偵訊。",
        quotes=[],
    )
    out = run_draft_lint_gate(
        job_id="j2b",
        draft=bad,
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.review_reason == ReviewReason.GROUNDING


# --- lint needs_revision -> NEEDS_REVISION -----------------------------------


def test_lint_failure_maps_to_needs_revision(store, audit, lint_config):
    _new_processing_job(store, "j3")
    # grounding passes, but the title is too long -> lint needs_revision
    bad = _good_draft(title="標" * 40)
    out = run_draft_lint_gate(
        job_id="j3",
        draft=bad,
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.grounding.passed
    assert out.job_state == JobState.NEEDS_REVISION
    assert store.get_job("j3").state == JobState.NEEDS_REVISION


def test_missing_video_section_with_videos_needs_revision(store, audit, lint_config):
    _new_processing_job(store, "j3v")
    out = run_draft_lint_gate(
        job_id="j3v",
        draft=_good_draft(),
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
        has_videos=True,
    )
    assert out.job_state == JobState.NEEDS_REVISION


# --- grounding precedence: when both fail, grounding wins ---------------------


def test_grounding_takes_precedence_over_lint(store, audit, lint_config):
    _new_processing_job(store, "j4")
    bad = _good_draft(
        title="標" * 40,  # lint would fail
        quotes=[SourceQuote(text="完全不存在於來源的捏造引述內容")],  # grounding fails
    )
    out = run_draft_lint_gate(
        job_id="j4",
        draft=bad,
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.job_state == JobState.NEEDS_HUMAN_REVIEW
    assert out.review_reason == ReviewReason.GROUNDING
    assert out.lint is None


# --- re-lint after grounding cleared (plan 架構審查 2d) ----------------------


def _job_at_grounding_review(store: JobStore, audit: AuditLog, job_id: str) -> None:
    """Drive a job to NEEDS_HUMAN_REVIEW(grounding) the legal way — through a
    grounding-fail gate run (the only path that reaches it via the PROCESSING
    edge)."""
    _new_processing_job(store, job_id)
    out = run_draft_lint_gate(
        job_id=job_id,
        draft=_good_draft(quotes=[SourceQuote(text="捏造不存在的引述內容")]),
        source_text=SOURCE,
        lint_config=build_lint_config(
            ContentConfig(
                title_min_chars=8,
                title_max_chars=35,
                intro_min_chars=1,
                intro_max_chars=9999,
                event_body_min_chars=1,
                event_body_max_chars=9999,
                summary_warn_chars=9998,
                summary_error_chars=9999,
                faq_min_count=1,
                faq_max_count=99,
                quick_facts_min_count=1,
                quick_facts_max_count=99,
            ),
            CATEGORIES,
        ),
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out.review_reason == ReviewReason.GROUNDING
    assert store.get_job(job_id).state == JobState.NEEDS_HUMAN_REVIEW


def test_relint_after_grounding_cleared_passes(store, audit, lint_config):
    # job sits at NEEDS_HUMAN_REVIEW(grounding); a human cleared the hold and
    # supplied a corrected, grounded draft. Re-lint must run; clean -> caller
    # drives NEEDS_HUMAN_REVIEW -> PROCESSED. The gate itself does not persist.
    _job_at_grounding_review(store, audit, "j5")
    out = relint_after_grounding_cleared(
        job_id="j5",
        draft=_good_draft(),
        source_text=SOURCE,
        lint_config=lint_config,
        audit=audit,
        ts=TS,
    )
    assert out.grounding is None  # grounding not re-evaluated
    assert out.lint.passed
    assert out.job_state is None  # caller drives NEEDS_HUMAN_REVIEW -> PROCESSED
    # The legal human clear is now possible because lint passed.
    rec = store.set_state("j5", JobState.PROCESSED, updated_at=TS)
    assert rec.state == JobState.PROCESSED


def test_relint_after_grounding_cleared_still_lints(store, audit, lint_config):
    # human cleared grounding but the draft still has a lint problem (bad tags).
    # Re-lint surfaces it; the gate does NOT push an illegal transition out of
    # NEEDS_HUMAN_REVIEW — the caller keeps the job for re-edit/supersede.
    _job_at_grounding_review(store, audit, "j6")
    out = relint_after_grounding_cleared(
        job_id="j6",
        draft=_good_draft(tags=["only-one"]),
        source_text=SOURCE,
        lint_config=lint_config,
        audit=audit,
        ts=TS,
    )
    assert out.lint.needs_revision
    assert out.job_state is None  # no silent illegal transition
    # state unchanged — still parked for the human (lint flagged the problem)
    assert store.get_job("j6").state == JobState.NEEDS_HUMAN_REVIEW


# --- audit is PII-free and hash-chained --------------------------------------


def test_gate_audit_is_pii_free_and_chained(store, audit, lint_config):
    _new_processing_job(store, "j7")
    run_draft_lint_gate(
        job_id="j7",
        draft=_good_draft(title="標" * 40),  # lint fail, grounding pass
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    lines = audit._read_lines()
    events = {l["event"] for l in lines}
    assert EVENT_GROUNDING_GATE in events
    assert EVENT_LINT_GATE in events
    for l in lines:
        extra = l.get("extra", {})
        # status/counts/score only — never raw title/body/url
        assert "title" not in extra and "body" not in extra and "url" not in extra
    assert audit.verify_chain()


def test_grounding_fail_audit_records_reason_code(store, audit, lint_config):
    _new_processing_job(store, "j8")
    run_draft_lint_gate(
        job_id="j8",
        draft=_good_draft(quotes=[SourceQuote(text="捏造的不存在引述")]),
        source_text=SOURCE,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    lines = audit._read_lines()
    g = [l for l in lines if l["event"] == EVENT_GROUNDING_GATE][-1]
    assert g["extra"]["review_reason"] == ReviewReason.GROUNDING.value
    assert g["extra"]["ungrounded_quote_count"] >= 1


# --- SECURITY: the gate never resolves a URL ---------------------------------


def test_gate_makes_no_network_request(store, audit, lint_config, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("draft_linter gate must not resolve/fetch a URL")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "gethostbyname", _boom)

    _new_processing_job(store, "j9")
    url_source = "http://169.254.169.254/latest http://attacker.test 內文。"
    d = _good_draft(
        event_body="http://attacker.test/x 應為純文字比對。",
        quotes=[SourceQuote(text="http://attacker.test/x")],
    )
    out = run_draft_lint_gate(  # must not raise
        job_id="j9",
        draft=d,
        source_text=url_source,
        lint_config=lint_config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert out is not None


def test_build_lint_config_projects_content_config():
    cfg = build_lint_config(
        ContentConfig(title_min_chars=25, title_max_chars=35, tag_min_count=3, tag_max_count=5),
        {"美食": [], "社會": []},
    )
    assert isinstance(cfg, LintConfig)
    assert cfg.title_min_chars == 25
    assert cfg.title_max_chars == 35
    assert set(cfg.categories) == {"美食", "社會"}
