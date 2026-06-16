"""Sign-off + responsibility-loop tests (Unit 8).

Cover: whitelist enforcement (+ audited rejection), attribution-not-auth
disclaimer + observed OS user, body-hash binding (editing the body after freeze
is detectable), approve -> APPROVED, backfill (attest required) -> recorded /
stays APPROVED without attest, supersede -> SUPERSEDED + SIGNOFF_INVALIDATED +
new-job link, and the state-machine gate (no path to APPROVED from
BLOCKED/DUPLICATE/NEEDS_HUMAN_REVIEW)."""

from __future__ import annotations

import pytest

from lcp.adapters.processor._persist import persist_gate_state
from lcp.adapters.publisher import signoff
from lcp.adapters.publisher.review_packet import build_review_packet
from lcp.adapters.publisher.signoff import (
    DISCLAIMER,
    EVENT_PUBLISHED_RECORDED,
    EVENT_SIGNOFF_APPROVE,
    EVENT_SIGNOFF_REJECT,
)
from lcp.adapters.storage.audit_log import (
    EVENT_SIGNOFF_INVALIDATED,
    EVENT_SUPERSEDED,
    AuditLog,
)
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config, PublisherConfig
from lcp.core.draft import Draft, FaqItem, SourceQuote
from lcp.core.errors import InputValidationError
from lcp.core.state import JobState, ReviewReason

TS = "2026-06-16T00:00:00Z"
REVIEWER = "alice"


@pytest.fixture()
def config():
    return Config(publisher=PublisherConfig(reviewers=[REVIEWER, "bob"]))


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


def _draft(**overrides) -> Draft:
    base = dict(
        title="台北華山美食市集週末熱鬧登場",
        intro="本週末在華山舉辦大型美食市集。",
        quick_facts=["時間：週末"],
        event_body="華山文創園區本週末舉辦美食市集。",
        faq=[FaqItem(question="要錢嗎？", answer="免費")],
        summary="不容錯過。",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    base.update(overrides)
    return Draft(**base)


def _review_pending_job(store, audit, job_id="j1", draft=None):
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, job_id, JobState.PROCESSED, updated_at=TS)
    return build_review_packet(
        job_id=job_id, draft=draft or _draft(), store=store, audit=audit,
        submitted_at=TS,
    )


# --- Happy path: approve -> APPROVED -> backfill -> PUBLISHED_RECORDED --------


def test_approve_then_backfill_completes_loop(config, store, audit):
    _review_pending_job(store, audit, "j1")

    rec = signoff.approve(
        "j1", REVIEWER, config=config, store=store, audit=audit, ts=TS,
    )
    assert rec.new_state is JobState.APPROVED
    assert store.get_job("j1").state is JobState.APPROVED
    assert rec.disclaimer == DISCLAIMER
    assert rec.observed_os_user  # some OS user observed

    # Not complete until backfilled (R37): still APPROVED, shows in worklist.
    assert store.get_job("j1").state is JobState.APPROVED

    new_state = signoff.backfill_published_url(
        "j1", "https://mysite.example/post/1",
        store=store, audit=audit, ts=TS, attested=True, reviewer=REVIEWER,
    )
    assert new_state is JobState.PUBLISHED_RECORDED
    assert store.get_job("j1").state is JobState.PUBLISHED_RECORDED


def test_approve_audit_binds_body_title_cover_hashes(config, store, audit):
    packet = _review_pending_job(store, audit, "jb")
    signoff.approve("jb", REVIEWER, config=config, store=store, audit=audit, ts=TS)

    evt = [l for l in audit._read_lines() if l["event"] == EVENT_SIGNOFF_APPROVE][-1]
    assert evt["artifact_sha256"] == packet.body_sha256  # body binding
    assert evt["extra"]["bound_title_sha256"] == packet.title_sha256
    assert evt["extra"]["disclaimer"] == DISCLAIMER
    assert evt["extra"]["reviewer_stated"] == REVIEWER
    assert evt["extra"]["observed_os_user"]


# --- Whitelist enforcement ---------------------------------------------------


def test_reviewer_not_in_whitelist_is_rejected_and_audited(config, store, audit):
    _review_pending_job(store, audit, "jw")
    with pytest.raises(InputValidationError):
        signoff.approve("jw", "mallory", config=config, store=store, audit=audit, ts=TS)
    # State unchanged; a rejection event was audited.
    assert store.get_job("jw").state is JobState.REVIEW_PENDING
    rejects = [l for l in audit._read_lines() if l["event"] == EVENT_SIGNOFF_REJECT]
    assert any(l["extra"]["reason"] == "reviewer_not_whitelisted" for l in rejects)


# --- Backfill without attestation stays APPROVED (loop open) ------------------


def test_backfill_without_attest_stays_approved(config, store, audit):
    _review_pending_job(store, audit, "jna")
    signoff.approve("jna", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError):
        signoff.backfill_published_url(
            "jna", "https://site.example/x",
            store=store, audit=audit, ts=TS, attested=False,
        )
    assert store.get_job("jna").state is JobState.APPROVED


def test_backfill_requires_nonempty_url(config, store, audit):
    _review_pending_job(store, audit, "ju")
    signoff.approve("ju", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError):
        signoff.backfill_published_url(
            "ju", "  ", store=store, audit=audit, ts=TS, attested=True,
        )
    assert store.get_job("ju").state is JobState.APPROVED


# --- Hash binding: editing the BODY after freeze is detectable ----------------


def test_body_edit_after_freeze_blocks_approval(config, store, audit):
    original = _draft()
    _review_pending_job(store, audit, "jh", draft=original)

    # An attacker/edit changes the body text after the packet froze.
    tampered = _draft(event_body="完全不同的正文，被竄改。")
    with pytest.raises(InputValidationError):
        signoff.approve(
            "jh", REVIEWER, config=config, store=store, audit=audit, ts=TS,
            draft=tampered,
        )
    assert store.get_job("jh").state is JobState.REVIEW_PENDING


def test_unchanged_body_passes_hash_binding(config, store, audit):
    original = _draft()
    _review_pending_job(store, audit, "jok", draft=original)
    # Re-passing the identical draft hashes to the same body -> allowed.
    rec = signoff.approve(
        "jok", REVIEWER, config=config, store=store, audit=audit, ts=TS,
        draft=original,
    )
    assert rec.new_state is JobState.APPROVED


# --- State machine: no path to APPROVED from blocked/duplicate/needs-review ---


@pytest.mark.parametrize(
    "target,reason",
    [
        (JobState.BLOCKED, None),
        (JobState.DUPLICATE, None),
        (JobState.NEEDS_HUMAN_REVIEW, ReviewReason.RISK),
    ],
)
def test_cannot_approve_blocked_duplicate_or_needs_review(
    config, store, audit, target, reason
):
    store.create_job("jx", created_at=TS)
    store.set_state("jx", JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, "jx", target, updated_at=TS, review_reason=reason)
    # No review packet freeze exists; even so, the whitelist passes and the state
    # machine (or the missing freeze) must refuse — there is no edge to APPROVED.
    with pytest.raises(InputValidationError):
        signoff.approve("jx", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    assert store.get_job("jx").state is target


# --- Supersede: APPROVED -> SUPERSEDED + SIGNOFF_INVALIDATED + new-job link ---


def test_supersede_approved_invalidates_signoff_and_links_new_job(config, store, audit):
    _review_pending_job(store, audit, "old")
    signoff.approve("old", REVIEWER, config=config, store=store, audit=audit, ts=TS)

    new_state = signoff.supersede(
        "old", store=store, audit=audit, ts=TS, new_job_id="new",
    )
    assert new_state is JobState.SUPERSEDED
    assert store.get_job("old").state is JobState.SUPERSEDED

    events = [l["event"] for l in audit._read_lines()]
    assert EVENT_SIGNOFF_INVALIDATED in events
    assert EVENT_SUPERSEDED in events
    sup = [l for l in audit._read_lines() if l["event"] == EVENT_SUPERSEDED][-1]
    assert sup["extra"]["new_job_id"] == "new"


def test_supersede_review_pending_is_allowed(config, store, audit):
    _review_pending_job(store, audit, "rp")
    new_state = signoff.supersede("rp", store=store, audit=audit, ts=TS, new_job_id="rp2")
    assert new_state is JobState.SUPERSEDED


def test_cannot_supersede_terminal_published(config, store, audit):
    _review_pending_job(store, audit, "pub")
    signoff.approve("pub", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    signoff.backfill_published_url(
        "pub", "https://x.example/1", store=store, audit=audit, ts=TS, attested=True,
    )
    with pytest.raises(InputValidationError):
        signoff.supersede("pub", store=store, audit=audit, ts=TS)


# --- Audit integrity throughout ---------------------------------------------


def test_audit_chain_verifies_after_full_loop(config, store, audit):
    _review_pending_job(store, audit, "j1")
    signoff.approve("j1", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    signoff.backfill_published_url(
        "j1", "https://x.example/1", store=store, audit=audit, ts=TS, attested=True,
    )
    assert audit.verify_chain()
    pub = [l for l in audit._read_lines() if l["event"] == EVENT_PUBLISHED_RECORDED][-1]
    assert pub["extra"]["operator_attested"] is True
    # URL itself is NOT in the audit (PII-free); only the recorded flag.
    assert "x.example" not in __import__("json").dumps(pub, ensure_ascii=False)
