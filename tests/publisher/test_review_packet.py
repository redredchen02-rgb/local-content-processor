"""Review packet tests (Unit 8).

Cover: a PROCESSED job -> a sanitized packet with body+title+cover hashes and
state REVIEW_PENDING; output-injection escaping; the freeze record shape; 0600
file modes; PII-free audit; and the freeze/edge-absence invariant (no in-place
re-run after a packet is built)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from lcp.adapters.publisher.review_packet import (
    EVENT_REVIEW_PACKET,
    build_review_packet,
    compute_body_sha256,
    read_review_manifest,
)
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.draft import Draft, FaqItem, MediaSection, SourceQuote
from lcp.core.errors import InputValidationError
from lcp.core.state import JobState

TS = "2026-06-16T00:00:00Z"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


def _processed_job(store: JobStore, job_id: str = "j1") -> None:
    """Drive a job to PROCESSED via the legal edges (the publisher unit assumes a
    PROCESSED job; the pipeline tests cover the gates that get it there)."""
    store.create_job(job_id, created_at=TS)
    store.set_state(job_id, JobState.CRAWLED, updated_at=TS)
    # PROCESSING is transient; persist PROCESSED via the gate seam.
    from lcp.adapters.processor._persist import persist_gate_state

    persist_gate_state(store, job_id, JobState.PROCESSED, updated_at=TS)


def _draft(**overrides) -> Draft:
    base = dict(
        title="台北華山美食市集週末熱鬧登場",
        intro="本週末在華山舉辦大型美食市集。",
        quick_facts=["時間：週末", "地點：華山"],
        event_body="華山文創園區本週末舉辦美食市集。現場有上百個攤位。",
        faq=[FaqItem(question="要錢嗎？", answer="免費入場")],
        summary="不容錯過的週末活動。",
        tags=["美食", "市集"],
        category="美食",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    base.update(overrides)
    return Draft(**base)


# --- Happy path --------------------------------------------------------------


def test_packet_builds_with_hashes_and_transitions_to_review_pending(store, audit):
    _processed_job(store, "j1")
    draft = _draft()

    packet = build_review_packet(
        job_id="j1", draft=draft, store=store, audit=audit, submitted_at=TS,
    )

    # State moved PROCESSED -> REVIEW_PENDING (the freeze point).
    assert store.get_job("j1").state is JobState.REVIEW_PENDING

    # All three freeze hashes recorded; body hash matches the recomputed one.
    assert packet.body_sha256 == compute_body_sha256(draft)
    assert len(packet.body_sha256) == 64
    assert len(packet.title_sha256) == 64

    # Files exist.
    assert packet.title_path.exists()
    assert packet.message_path.exists()
    assert packet.manifest_path.exists()

    # Freeze record on disk matches.
    manifest = read_review_manifest(store, "j1")
    assert manifest["review_status"] == "pending"
    assert manifest["freeze"]["body_sha256"] == packet.body_sha256
    assert manifest["freeze"]["title_sha256"] == packet.title_sha256
    assert manifest["submitted_at"] == TS
    assert manifest["encryption"] is False  # honest: no encryption claim
    assert manifest["deletion"] == "best_effort"


def test_packet_copies_cover_and_hashes_it(store, audit, tmp_path):
    _processed_job(store, "jc")
    cover_src = tmp_path / "processed_cover.jpg"
    cover_src.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

    packet = build_review_packet(
        job_id="jc", draft=_draft(), store=store, audit=audit,
        submitted_at=TS, processed_cover=cover_src,
    )
    assert packet.cover_path is not None and packet.cover_path.exists()
    assert packet.cover_sha256 is not None and len(packet.cover_sha256) == 64
    manifest = read_review_manifest(store, "jc")
    assert manifest["freeze"]["cover_sha256"] == packet.cover_sha256


def test_packet_without_cover_records_null_cover_hash(store, audit):
    _processed_job(store, "jn")
    packet = build_review_packet(
        job_id="jn", draft=_draft(), store=store, audit=audit, submitted_at=TS,
    )
    assert packet.cover_sha256 is None
    assert read_review_manifest(store, "jn")["freeze"]["cover_sha256"] is None


# --- Output injection: <script> must be escaped (inert) ----------------------


def test_script_in_title_and_body_is_escaped_in_packet(store, audit):
    _processed_job(store, "jx")
    draft = _draft(
        title="<script>alert(1)</script>惡意標題",
        event_body="正文 <img src=x onerror=alert(2)> 結束",
    )
    packet = build_review_packet(
        job_id="jx", draft=draft, store=store, audit=audit, submitted_at=TS,
    )

    title_text = packet.title_path.read_text(encoding="utf-8")
    msg_text = packet.message_path.read_text(encoding="utf-8")

    # The raw executable markup must NOT appear; the escaped entities must.
    assert "<script>" not in title_text
    assert "&lt;script&gt;" in title_text
    assert "<img src=x onerror=" not in msg_text
    assert "&lt;img" in msg_text


def test_source_urls_render_as_inert_text(store, audit):
    _processed_job(store, "ju")
    packet = build_review_packet(
        job_id="ju", draft=_draft(), store=store, audit=audit, submitted_at=TS,
        source_urls=["https://example.com/<script>", "javascript:alert(1)"],
    )
    msg = packet.message_path.read_text(encoding="utf-8")
    # No clickable anchor, and the dangerous markup is escaped.
    assert "<a " not in msg
    assert "&lt;script&gt;" in msg
    manifest = read_review_manifest(store, "ju")
    assert all("<script>" not in u for u in manifest["source_links_inert"])


# --- File modes 0600 ---------------------------------------------------------


def test_packet_files_are_0600(store, audit, tmp_path):
    _processed_job(store, "jm")
    cover_src = tmp_path / "c.jpg"
    cover_src.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    packet = build_review_packet(
        job_id="jm", draft=_draft(), store=store, audit=audit,
        submitted_at=TS, processed_cover=cover_src,
    )
    for p in (packet.title_path, packet.message_path, packet.manifest_path,
              packet.cover_path):
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == 0o600, f"{p} mode {oct(mode)} != 0600"


# --- Audit is PII-free and carries the high-entropy artifact hash ------------


def test_audit_event_is_pii_free_and_carries_body_hash(store, audit):
    _processed_job(store, "ja")
    packet = build_review_packet(
        job_id="ja", draft=_draft(), store=store, audit=audit, submitted_at=TS,
    )
    assert audit.verify_chain()
    lines = audit._read_lines()
    evt = [l for l in lines if l["event"] == EVENT_REVIEW_PACKET][-1]
    assert evt["artifact_sha256"] == packet.body_sha256
    # No raw title/body smuggled in.
    blob = json.dumps(evt, ensure_ascii=False)
    assert "華山" not in blob


# --- Freeze / state invariants ----------------------------------------------


def test_packet_refuses_non_processed_job(store, audit):
    store.create_job("jp", created_at=TS)
    store.set_state("jp", JobState.CRAWLED, updated_at=TS)
    with pytest.raises(InputValidationError):
        build_review_packet(
            job_id="jp", draft=_draft(), store=store, audit=audit, submitted_at=TS,
        )


def test_no_in_place_rerun_after_freeze(store, audit):
    """After REVIEW_PENDING there is intentionally no edge back to PROCESSING —
    the draft is frozen (freeze via edge-absence)."""
    _processed_job(store, "jf")
    build_review_packet(
        job_id="jf", draft=_draft(), store=store, audit=audit, submitted_at=TS,
    )
    # The persist seam validates PROCESSING -> target only from a legal
    # predecessor; REVIEW_PENDING is not one, so re-processing is impossible.
    from lcp.adapters.processor._persist import persist_gate_state

    with pytest.raises(InputValidationError):
        persist_gate_state(store, "jf", JobState.PROCESSED, updated_at=TS)


# --- media-produced cover is auto-wired into the freeze -----------------------


def test_cover_auto_picked_up_from_processed_dir(store, audit):
    """The Stage-2 media gate writes processed/cover/cover.jpg; build_review_packet
    picks it up WITHOUT an explicit processed_cover and binds its hash."""
    _processed_job(store, "jc")
    job_dir = store.job_dir("jc")
    cover = job_dir / "processed" / "cover" / "cover.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes-for-hashing")

    packet = build_review_packet(
        job_id="jc", draft=_draft(), store=store, audit=audit,
        submitted_at=TS, source_urls=[],
    )
    assert packet.cover_sha256 is not None
    assert packet.cover_path is not None and packet.cover_path.exists()
    assert (job_dir / "review" / "cover.jpg").exists()


def test_no_cover_when_absent_stays_none(store, audit):
    """No processed cover on disk -> cover hash stays None (unchanged behaviour)."""
    _processed_job(store, "jn")
    packet = build_review_packet(
        job_id="jn", draft=_draft(), store=store, audit=audit,
        submitted_at=TS, source_urls=[],
    )
    assert packet.cover_sha256 is None
    assert packet.cover_path is None
