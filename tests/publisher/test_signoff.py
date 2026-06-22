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
from lcp.adapters.processor.risk_checker import run_risk_gate
from lcp.adapters.publisher import signoff
from lcp.adapters.publisher.review_packet import build_review_packet
from lcp.adapters.publisher.signoff import (
    DISCLAIMER,
    EVENT_PUBLISHED_RECORDED,
    EVENT_SIGNOFF_APPROVE,
    EVENT_SIGNOFF_REJECT,
)
from lcp.adapters.storage.audit_log import (
    EVENT_REDLINE_OVERRIDE,
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
    from lcp.pipeline import save_draft as _save_draft

    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, job_id, JobState.PROCESSED, updated_at=TS)
    d = draft or _draft()
    # Write draft.json so approve(draft=None) can load and verify the hash.
    _save_draft(store, job_id, d)
    return build_review_packet(
        job_id=job_id,
        draft=d,
        store=store,
        audit=audit,
        submitted_at=TS,
    )


# --- Happy path: approve -> APPROVED -> backfill -> PUBLISHED_RECORDED --------


def test_approve_then_backfill_completes_loop(config, store, audit):
    _review_pending_job(store, audit, "j1")

    rec = signoff.approve(
        "j1",
        REVIEWER,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert rec.new_state is JobState.APPROVED
    assert store.get_job("j1").state is JobState.APPROVED
    assert rec.disclaimer == DISCLAIMER
    assert rec.observed_os_user  # some OS user observed

    # Not complete until backfilled (R37): still APPROVED, shows in worklist.
    assert store.get_job("j1").state is JobState.APPROVED

    new_state = signoff.backfill_published_url(
        "j1",
        "https://mysite.example/post/1",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer=REVIEWER,
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
            "jna",
            "https://site.example/x",
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            attested=False,
            reviewer=REVIEWER,
        )
    assert store.get_job("jna").state is JobState.APPROVED


def test_backfill_requires_nonempty_url(config, store, audit):
    _review_pending_job(store, audit, "ju")
    signoff.approve("ju", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError):
        signoff.backfill_published_url(
            "ju",
            "  ",
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            attested=True,
            reviewer=REVIEWER,
        )
    assert store.get_job("ju").state is JobState.APPROVED


def test_backfill_non_whitelisted_reviewer_rejected(config, store, audit):
    """P3 regression: backfill requires a whitelisted reviewer like approve."""
    _review_pending_job(store, audit, "jbw")
    signoff.approve("jbw", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    with pytest.raises(InputValidationError):
        signoff.backfill_published_url(
            "jbw",
            "https://site.example/x",
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            attested=True,
            reviewer="mallory",
        )
    assert store.get_job("jbw").state is JobState.APPROVED


# --- Hash binding: editing the BODY after freeze is detectable ----------------


def test_body_edit_after_freeze_blocks_approval(config, store, audit):
    original = _draft()
    _review_pending_job(store, audit, "jh", draft=original)

    # An attacker/edit changes the body text after the packet froze.
    tampered = _draft(event_body="完全不同的正文，被竄改。")
    with pytest.raises(InputValidationError):
        signoff.approve(
            "jh",
            REVIEWER,
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            draft=tampered,
        )
    assert store.get_job("jh").state is JobState.REVIEW_PENDING


def test_approve_refuses_freeze_missing_bound_hash(config, store, audit):
    """Fail closed: a malformed freeze (missing the bound body/title hash) must
    not yield an approval bound to a null hash. Regression for the mypy-surfaced
    Any|None -> str finding in signoff.approve."""
    import json

    from lcp.adapters.publisher.review_packet import REVIEW_MANIFEST_NAME

    _review_pending_job(store, audit, "jmf")
    mpath = store.job_dir("jmf") / "review" / REVIEW_MANIFEST_NAME
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    manifest["freeze"]["body_sha256"] = None  # corrupt/incomplete freeze
    manifest["freeze"].pop("title_sha256", None)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(InputValidationError):
        signoff.approve("jmf", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    # Not approved — state unchanged.
    assert store.get_job("jmf").state is JobState.REVIEW_PENDING


def test_unchanged_body_passes_hash_binding(config, store, audit):
    original = _draft()
    _review_pending_job(store, audit, "jok", draft=original)
    # Re-passing the identical draft hashes to the same body -> allowed.
    rec = signoff.approve(
        "jok",
        REVIEWER,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        draft=original,
    )
    assert rec.new_state is JobState.APPROVED


# --- U3: editing the TITLE or COVER after freeze is detectable ----------------


def test_title_edit_after_freeze_blocks_approval(config, store, audit):
    """U3: editing ONLY the title after freeze (body unchanged) must be detected.
    Previously only the body hash was re-verified, so a title swap slipped through
    and the audit falsely attested the original (reviewer-approved) title."""
    original = _draft()
    _review_pending_job(store, audit, "jt", draft=original)
    # Same body, DIFFERENT title -> passes the body check, must fail the title check.
    tampered = _draft(title="完全不同的標題，已被竄改")
    with pytest.raises(InputValidationError):
        signoff.approve(
            "jt",
            REVIEWER,
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            draft=tampered,
        )
    assert store.get_job("jt").state is JobState.REVIEW_PENDING


def test_cover_edit_after_freeze_blocks_approval(config, store, audit):
    """U3: swapping the frozen review-dir cover after freeze must be detected."""
    job_id = "jc"
    cover_src = store.job_dir(job_id) / "processed" / "cover" / "cover.jpg"
    cover_src.parent.mkdir(parents=True, exist_ok=True)
    cover_src.write_bytes(b"original-cover-bytes")
    _review_pending_job(store, audit, job_id)  # freezes the cover sha
    # Swap the review-dir cover the freeze bound to.
    review_cover = store.job_dir(job_id) / "review" / "cover.jpg"
    review_cover.write_bytes(b"tampered-cover-bytes-which-differ")
    with pytest.raises(InputValidationError):
        signoff.approve(job_id, REVIEWER, config=config, store=store, audit=audit, ts=TS)
    assert store.get_job(job_id).state is JobState.REVIEW_PENDING


def test_unchanged_cover_passes(config, store, audit):
    """A job with an untouched frozen cover still approves cleanly."""
    job_id = "jcc"
    cover_src = store.job_dir(job_id) / "processed" / "cover" / "cover.jpg"
    cover_src.parent.mkdir(parents=True, exist_ok=True)
    cover_src.write_bytes(b"stable-cover-bytes")
    _review_pending_job(store, audit, job_id)
    rec = signoff.approve(
        job_id,
        REVIEWER,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
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
def test_cannot_approve_blocked_duplicate_or_needs_review(config, store, audit, target, reason):
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
        "old",
        store=store,
        audit=audit,
        ts=TS,
        new_job_id="new",
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
        "pub",
        "https://x.example/1",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer=REVIEWER,
    )
    with pytest.raises(InputValidationError):
        signoff.supersede("pub", store=store, audit=audit, ts=TS)


# --- U8: operator recovery of a false-terminal BLOCKED / DUPLICATE -----------


def _blocked_via_risk_gate(store, audit, job_id):
    """Drive a job to a REAL BLOCKED via the risk gate, so a genuine RISK_GATE
    audit event (with flag_categories) exists for the override to recover."""
    from lcp.core.rules.risk_rules import RiskInput

    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    store.mark_processing(job_id)
    outcome = run_risk_gate(
        job_id=job_id,
        # '未成年' is a hard MINOR redline keyword -> terminal BLOCKED.
        content=RiskInput(title="某新聞", body="涉及未成年的私密內容"),
        store=store,
        audit=audit,
        ts=TS,
    )
    assert outcome.job_state is JobState.BLOCKED
    assert store.get_job(job_id).state is JobState.BLOCKED


def _duplicate_job(store, job_id):
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, job_id, JobState.DUPLICATE, updated_at=TS)


def test_blocked_recovery_requires_redline_override(config, store, audit):
    # A BLOCKED supersede WITHOUT the second confirmation is refused (the
    # ordinary abandon path may not be reused for a redline state).
    _blocked_via_risk_gate(store, audit, "jb")
    with pytest.raises(InputValidationError):
        signoff.supersede("jb", store=store, audit=audit, ts=TS, actor="alice")
    assert store.get_job("jb").state is JobState.BLOCKED


def test_blocked_recovery_with_override_emits_redline_override_event(config, store, audit):
    _blocked_via_risk_gate(store, audit, "jb")
    new_state = signoff.supersede(
        "jb",
        store=store,
        audit=audit,
        ts=TS,
        actor="alice",
        redline_override=True,
        new_job_id="jb2",
    )
    assert new_state is JobState.SUPERSEDED
    assert store.get_job("jb").state is JobState.SUPERSEDED

    events = [l["event"] for l in audit._read_lines()]
    # Distinct event TYPE (not the ordinary SUPERSEDED), and NO false
    # "void the old sign-off" (a BLOCKED job was never signed off).
    assert EVENT_REDLINE_OVERRIDE in events
    assert EVENT_SUPERSEDED not in events
    assert EVENT_SIGNOFF_INVALIDATED not in events

    override = [l for l in audit._read_lines() if l["event"] == EVENT_REDLINE_OVERRIDE][-1]
    # Real actor recorded (not the "human" literal default).
    assert override["actor"] == "alice"
    # blocking_reasons sourced from the prior RISK_GATE event, as enum CODES only.
    assert override["extra"]["blocking_reasons"] == ["minor"]
    assert override["extra"]["new_job_id"] == "jb2"


def test_duplicate_recovery_is_ordinary_single_step(config, store, audit):
    # DUPLICATE is not a redline state -> ordinary single-step confirm, no
    # override flag needed, ordinary SUPERSEDED event, and NO SIGNOFF_INVALIDATED
    # (it was never signed off).
    _duplicate_job(store, "jd")
    new_state = signoff.supersede(
        "jd",
        store=store,
        audit=audit,
        ts=TS,
        actor="alice",
        new_job_id="jd2",
    )
    assert new_state is JobState.SUPERSEDED
    events = [l["event"] for l in audit._read_lines()]
    assert EVENT_SUPERSEDED in events
    assert EVENT_REDLINE_OVERRIDE not in events
    assert EVENT_SIGNOFF_INVALIDATED not in events


def test_blocked_supersede_refuses_without_supersedable_extension(
    monkeypatch, config, store, audit
):
    # Regression guard: the state-table edge alone is NOT enough — `supersede`
    # independently gates on _SUPERSEDABLE. If BLOCKED were dropped from the
    # frozenset, even a correct override gesture must still refuse.
    _blocked_via_risk_gate(store, audit, "jb")
    narrowed = signoff._SUPERSEDABLE - {JobState.BLOCKED, JobState.DUPLICATE}
    monkeypatch.setattr(signoff, "_SUPERSEDABLE", narrowed)
    with pytest.raises(InputValidationError):
        signoff.supersede(
            "jb",
            store=store,
            audit=audit,
            ts=TS,
            actor="alice",
            redline_override=True,
        )


# --- NEEDS_HUMAN_REVIEW is not a dead-end (resolve / reject) -----------------


def _nhr_job(store, job_id, reason, *, draft=None, source_text=None):
    """Drive a job to NEEDS_HUMAN_REVIEW with the given hold reason. Optionally
    persist a draft + source.txt (for the grounding re-lint path)."""
    from lcp.pipeline import save_draft

    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    if source_text is not None:
        raw = store.job_dir(job_id) / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text(source_text, encoding="utf-8")
    if draft is not None:
        save_draft(store, job_id, draft)
    persist_gate_state(
        store,
        job_id,
        JobState.NEEDS_HUMAN_REVIEW,
        updated_at=TS,
        review_reason=reason,
    )


@pytest.mark.parametrize("reason", [ReviewReason.RISK, ReviewReason.DEDUP])
def test_resolve_risk_dedup_hold_via_override(config, store, audit, reason):
    """A risk/dedup hold clears to PROCESSED via an explicit, audited override."""
    from lcp.adapters.publisher.signoff import EVENT_NHR_RESOLVED

    _nhr_job(store, "jr", reason)
    rec = signoff.resolve(
        "jr",
        REVIEWER,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        reason="manually reviewed, false positive",
    )
    assert rec.new_state is JobState.PROCESSED
    assert store.get_job("jr").state is JobState.PROCESSED
    evt = [l for l in audit._read_lines() if l["event"] == EVENT_NHR_RESOLVED][-1]
    assert evt["extra"]["mode"] == "human_override"
    assert evt["extra"]["resolved_from_reason"] == reason.value
    assert evt["extra"]["override_note"]


def test_resolve_override_requires_reason(config, store, audit):
    _nhr_job(store, "jor", ReviewReason.RISK)
    with pytest.raises(InputValidationError):
        signoff.resolve(
            "jor",
            REVIEWER,
            config=config,
            store=store,
            audit=audit,
            ts=TS,
        )
    assert store.get_job("jor").state is JobState.NEEDS_HUMAN_REVIEW


def _lint_clean_draft(**overrides) -> Draft:
    """A draft that PASSES the default lint (title 25-35 chars, all required
    sections incl. image_sections, 3-5 objective tags)."""
    from lcp.core.draft import MediaSection

    base = dict(
        title="台北華山文創園區週末美食市集熱鬧登場活動報導特別企劃專題",  # 28 chars (25-35)
        intro="本週末在華山舉辦大型美食市集。",
        quick_facts=["時間：週末", "地點：華山"],
        event_body="華山文創園區本週末舉辦美食市集，現場人潮眾多。",
        image_sections=[MediaSection(asset_ref="raw/images/a.jpg", caption="現場")],
        faq=[FaqItem(question="要錢嗎？", answer="免費入場")],
        summary="不容錯過的週末活動。",
        tags=["美食", "市集", "華山"],
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    base.update(overrides)
    return Draft(**base)


def test_resolve_grounding_hold_relint_clean_promotes(config, store, audit):
    """A grounding hold clears via re-lint: a clean lint promotes to PROCESSED."""
    source = "華山文創園區本週末舉辦美食市集。"
    draft = _lint_clean_draft()
    # sanity: this draft really passes the default lint
    assert len(draft.title) >= 25 and len(draft.title) <= 35
    _nhr_job(store, "jg", ReviewReason.GROUNDING, draft=draft, source_text=source)
    rec = signoff.resolve(
        "jg",
        REVIEWER,
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        relint=True,
    )
    assert rec.new_state is JobState.PROCESSED
    assert store.get_job("jg").state is JobState.PROCESSED
    # U5: the re-lint emits exactly ONE LINT_GATE event, with the resolving
    # reviewer as actor (NOT a literal "human") — the accountability identity.
    from lcp.adapters.processor.draft_linter import EVENT_LINT_GATE

    lint_events = [l for l in audit._read_lines() if l["event"] == EVENT_LINT_GATE]
    assert len(lint_events) == 1
    assert lint_events[0]["actor"] == REVIEWER


def test_resolve_grounding_relint_dirty_lint_refuses(config, store, audit):
    """If the re-lint still fails, resolve refuses and the job stays held."""
    # A too-short title fails lint -> not promoted.
    source = "華山文創園區本週末舉辦美食市集。"
    draft = _draft(title="短")  # below title_min_chars -> lint fails
    _nhr_job(store, "jgd", ReviewReason.GROUNDING, draft=draft, source_text=source)
    with pytest.raises(InputValidationError) as ei:
        signoff.resolve(
            "jgd",
            REVIEWER,
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            relint=True,
        )
    # U5: signoff keeps the exact operator-facing refusal (message + exit code);
    # only the lint PASS/refuse verdict moved into the processor boolean.
    assert "re-lint still fails" in str(ei.value)
    assert ei.value.exit_code == 2
    assert store.get_job("jgd").state is JobState.NEEDS_HUMAN_REVIEW


@pytest.mark.parametrize("reason", [ReviewReason.RISK, ReviewReason.DEDUP, ReviewReason.GROUNDING])
def test_reject_nhr_without_freeze_reaches_rejected(config, store, audit, reason):
    """A NEEDS_HUMAN_REVIEW job (no packet/freeze) can be rejected -> REJECTED.

    Previously reject() called _freeze_hashes() which raises for jobs without a
    packet, dead-ending NHR jobs."""
    from lcp.adapters.publisher.signoff import EVENT_SIGNOFF_REJECT

    _nhr_job(store, "jrej", reason)
    rec = signoff.reject(
        "jrej",
        REVIEWER,
        "not suitable",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
    )
    assert rec.new_state is JobState.REJECTED
    assert store.get_job("jrej").state is JobState.REJECTED
    evt = [l for l in audit._read_lines() if l["event"] == EVENT_SIGNOFF_REJECT][-1]
    assert evt["extra"]["rejected_from_state"] == JobState.NEEDS_HUMAN_REVIEW.value


def test_resolve_requires_nhr_state(config, store, audit):
    _review_pending_job(store, audit, "jrp")
    with pytest.raises(InputValidationError):
        signoff.resolve(
            "jrp",
            REVIEWER,
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            reason="x",
        )


def test_resolve_non_whitelisted_rejected(config, store, audit):
    _nhr_job(store, "jnw", ReviewReason.RISK)
    with pytest.raises(InputValidationError):
        signoff.resolve(
            "jnw",
            "mallory",
            config=config,
            store=store,
            audit=audit,
            ts=TS,
            reason="x",
        )
    assert store.get_job("jnw").state is JobState.NEEDS_HUMAN_REVIEW


def test_supersede_nhr_is_allowed(config, store, audit):
    _nhr_job(store, "jsup", ReviewReason.DEDUP)
    new_state = signoff.supersede(
        "jsup",
        store=store,
        audit=audit,
        ts=TS,
        new_job_id="jsup2",
    )
    assert new_state is JobState.SUPERSEDED
    assert store.get_job("jsup").state is JobState.SUPERSEDED


def test_supersede_nhr_does_not_invalidate_signoff(config, store, audit):
    # bug_008: NEEDS_HUMAN_REVIEW is reached BEFORE the freeze/review-packet step,
    # so it never carried a sign-off. Superseding it must NOT emit a (false)
    # SIGNOFF_INVALIDATED — matches the supersede docstring's "only REVIEW_PENDING /
    # APPROVED carry a real prior sign-off" contract.
    _nhr_job(store, "jnhr", ReviewReason.DEDUP)
    signoff.supersede("jnhr", store=store, audit=audit, ts=TS, new_job_id="jnhr2")
    events = [l["event"] for l in audit._read_lines()]
    assert EVENT_SUPERSEDED in events
    assert EVENT_SIGNOFF_INVALIDATED not in events


def test_supersede_needs_revision_does_not_invalidate_signoff(config, store, audit):
    # bug_008: NEEDS_REVISION likewise never carried a sign-off (Stage-2 hold, pre-freeze).
    store.create_job("jnr", created_at=TS)
    store.set_state("jnr", JobState.CRAWLED, updated_at=TS)
    persist_gate_state(store, "jnr", JobState.NEEDS_REVISION, updated_at=TS)
    signoff.supersede("jnr", store=store, audit=audit, ts=TS, new_job_id="jnr2")
    events = [l["event"] for l in audit._read_lines()]
    assert EVENT_SUPERSEDED in events
    assert EVENT_SIGNOFF_INVALIDATED not in events


# --- Audit integrity throughout ---------------------------------------------


def test_audit_chain_verifies_after_full_loop(config, store, audit):
    _review_pending_job(store, audit, "j1")
    signoff.approve("j1", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    signoff.backfill_published_url(
        "j1",
        "https://x.example/1",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer=REVIEWER,
    )
    assert audit.verify_chain()
    pub = [l for l in audit._read_lines() if l["event"] == EVENT_PUBLISHED_RECORDED][-1]
    assert pub["extra"]["operator_attested"] is True
    # URL itself is NOT in the audit (PII-free); only the recorded flag.
    assert "x.example" not in __import__("json").dumps(pub, ensure_ascii=False)


# --- U5: backfill atomic write + single observed_os_user --------------------


def test_backfill_observed_os_user_called_once(config, store, audit, monkeypatch):
    """F3: observed_os_user() must be called ONCE and the same value used for
    actor= and extra[observed_os_user]. Two calls can race and produce different
    values, or accumulate two syscalls unnecessarily."""
    calls = []

    def _fake_os_user():
        calls.append("call")
        return "test-operator"

    monkeypatch.setattr(signoff, "observed_os_user", _fake_os_user)
    _review_pending_job(store, audit, "ju5")
    signoff.approve("ju5", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    signoff.backfill_published_url(
        "ju5",
        "https://x.example/1",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer=REVIEWER,
    )
    # Count calls made BY backfill_published_url specifically (approve also calls it).
    # After approve, the count should be N; after backfill it should be N+1 (once).
    # Reset counter for isolation
    calls.clear()
    calls.clear()
    _review_pending_job(store, audit, "ju5b")
    signoff.approve("ju5b", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    calls.clear()
    signoff.backfill_published_url(
        "ju5b",
        "https://x.example/2",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer=REVIEWER,
    )
    assert len(calls) == 1, f"observed_os_user() called {len(calls)} times in backfill; expected 1"


def test_backfill_url_file_is_atomic(config, store, audit, tmp_path, monkeypatch):
    """F2: published_url.txt must be written atomically (temp + rename) so a
    crash mid-write never leaves a partial URL in the file."""
    import os

    writes = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        writes.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)
    _review_pending_job(store, audit, "ju5c")
    signoff.approve("ju5c", REVIEWER, config=config, store=store, audit=audit, ts=TS)
    signoff.backfill_published_url(
        "ju5c",
        "https://x.example/3",
        config=config,
        store=store,
        audit=audit,
        ts=TS,
        attested=True,
        reviewer=REVIEWER,
    )
    # An atomic write uses os.replace(tmp, dst); a non-atomic open('w') uses none.
    url_writes = [(s, d) for s, d in writes if "published_url" in str(d)]
    assert url_writes, "backfill did not use os.replace() for published_url.txt — not atomic"


# --- U6: approve fail-loud when draft.json is missing -----------------------


def test_approve_raises_when_draft_json_missing(config, store, audit):
    """F6: approve() with draft=None must raise if load_draft returns None
    (draft.json missing/corrupt), not silently skip the hash binding check."""
    _review_pending_job(store, audit, "ju6")
    # Delete the draft.json to simulate a missing/corrupt draft file.
    # draft.json lives at processed/draft.json inside the job dir.
    draft_json = store.job_dir("ju6") / "processed" / "draft.json"
    assert draft_json.exists(), f"expected draft.json at {draft_json}"
    draft_json.unlink()

    with pytest.raises(InputValidationError, match="draft"):
        signoff.approve("ju6", REVIEWER, config=config, store=store, audit=audit, ts=TS)
