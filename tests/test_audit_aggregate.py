"""Unit 1: pure audit aggregation + torn-line-tolerant reader."""

from lcp.adapters.storage.audit_aggregate import (
    EVENT_DEDUP_GATE,
    EVENT_GROUNDING_GATE,
    EVENT_LINT_GATE,
    EVENT_RISK_GATE,
    aggregate_audit,
)
from lcp.adapters.storage.audit_log import AuditLog


def test_gate_event_constants_match_processor_definitions():
    """Drift guard: the local mirrors must equal the processor source of truth."""
    from lcp.adapters.processor.dedup_checker import EVENT_DEDUP_GATE as DEDUP
    from lcp.adapters.processor.draft_linter import (
        EVENT_GROUNDING_GATE as GROUND,
        EVENT_LINT_GATE as LINT,
    )
    from lcp.adapters.processor.media_checker import EVENT_MEDIA_GATE as MEDIA
    from lcp.adapters.processor.risk_checker import EVENT_RISK_GATE as RISK
    from lcp.adapters.storage import audit_aggregate as agg

    assert agg.EVENT_RISK_GATE == RISK
    assert agg.EVENT_DEDUP_GATE == DEDUP
    assert agg.EVENT_LINT_GATE == LINT
    assert agg.EVENT_GROUNDING_GATE == GROUND
    assert agg.EVENT_MEDIA_GATE == MEDIA


def _gate(seq, gate, job_id, status, *, ts="2026-06-16T00:00:00Z", stage="risk",
          review_reason=None):
    extra = {"status": status}
    if review_reason is not None:
        extra["review_reason"] = review_reason
    return {
        "seq": seq, "ts": ts, "stage": stage, "event": gate,
        "job_id": job_id, "actor": "machine", "extra": extra,
    }


def test_intercept_rates_with_reached_denominator():
    events = [
        _gate(0, EVENT_RISK_GATE, "j1", "pass"),
        _gate(1, EVENT_RISK_GATE, "j2", "blocked", review_reason="risk"),
        _gate(2, EVENT_RISK_GATE, "j3", "needs_human_review", review_reason="risk"),
        # only j1 survived risk and reached grounding
        _gate(3, EVENT_GROUNDING_GATE, "j1", "pass", stage="lint"),
    ]
    s = aggregate_audit(events)
    by_gate = {g.gate: g for g in s.gates}
    # risk: 3 reached, 2 intercepted
    assert by_gate[EVENT_RISK_GATE].reached == 3
    assert by_gate[EVENT_RISK_GATE].intercepted == 2
    assert abs(by_gate[EVENT_RISK_GATE].rate - 2 / 3) < 1e-9
    # grounding denominator is the 1 job that REACHED it, not all 3
    assert by_gate[EVENT_GROUNDING_GATE].reached == 1
    assert by_gate[EVENT_GROUNDING_GATE].intercepted == 0
    assert by_gate[EVENT_GROUNDING_GATE].rate == 0.0


def test_review_reason_counts():
    events = [
        _gate(0, EVENT_RISK_GATE, "j1", "blocked", review_reason="risk"),
        _gate(1, EVENT_DEDUP_GATE, "j2", "duplicate", stage="dedup",
              review_reason="dedup"),
        _gate(2, EVENT_RISK_GATE, "j3", "blocked", review_reason="risk"),
    ]
    s = aggregate_audit(events)
    assert s.review_reasons == {"risk": 2, "dedup": 1}


def test_dedup_unique_is_pass_not_intercept():
    s = aggregate_audit([_gate(0, EVENT_DEDUP_GATE, "j1", "unique", stage="dedup")])
    by_gate = {g.gate: g for g in s.gates}
    assert by_gate[EVENT_DEDUP_GATE].intercepted == 0


def test_empty_events_returns_zeroed_summary_no_error():
    s = aggregate_audit([])
    assert s.gates == []
    assert s.review_reasons == {}
    assert s.gate_gaps == []
    assert s.daily_jobs == {}


def test_zero_reached_gate_has_none_rate():
    # a gate with no events simply doesn't appear; rate is None only via GateStat
    from lcp.adapters.storage.audit_aggregate import GateStat

    assert GateStat(gate=EVENT_LINT_GATE, reached=0, intercepted=0).rate is None


def test_gate_gaps_grouped_per_job_not_cross_job():
    # Two jobs interleaved in the global stream; gaps must not cross job_id.
    events = [
        _gate(0, EVENT_RISK_GATE, "j1", "pass", ts="2026-06-16T00:00:00Z"),
        _gate(1, EVENT_RISK_GATE, "j2", "pass", ts="2026-06-16T05:00:00Z"),
        _gate(2, EVENT_GROUNDING_GATE, "j1", "pass", stage="lint",
              ts="2026-06-16T00:00:30Z"),
    ]
    s = aggregate_audit(events)
    # only the j1 risk->grounding pair yields a gap; j2 has a single event
    assert len(s.gate_gaps) == 1
    gap = s.gate_gaps[0]
    assert gap.job_id == "j1"
    assert gap.seconds == 30.0


def test_single_event_job_yields_no_gap():
    s = aggregate_audit([_gate(0, EVENT_RISK_GATE, "j1", "pass")])
    assert s.gate_gaps == []


def test_daily_buckets_split_by_date():
    events = [
        _gate(0, EVENT_RISK_GATE, "j1", "pass", ts="2026-06-16T23:59:00Z"),
        _gate(1, EVENT_RISK_GATE, "j2", "pass", ts="2026-06-17T00:01:00Z"),
        _gate(2, EVENT_LINT_GATE, "j1", "pass", stage="lint",
              ts="2026-06-16T23:59:30Z"),
    ]
    s = aggregate_audit(events)
    assert s.daily_jobs == {"2026-06-16": 1, "2026-06-17": 1}


def test_unknown_event_and_missing_fields_are_skipped():
    events = [
        {"seq": 0, "ts": "2026-06-16T00:00:00Z", "event": "CRAWL_OK",
         "job_id": "j1", "actor": "m"},  # not a gate
        {"event": EVENT_RISK_GATE},  # missing job_id/ts
        _gate(2, EVENT_RISK_GATE, "j2", "blocked", review_reason="risk"),
    ]
    s = aggregate_audit(events)
    by_gate = {g.gate: g for g in s.gates}
    assert by_gate[EVENT_RISK_GATE].reached == 1


def test_summarize_gaps_rolls_up_per_transition_dropping_job_id():
    from lcp.adapters.storage.audit_aggregate import GateGap, summarize_gaps

    gaps = [
        GateGap("j1", "risk", "lint", 10.0),
        GateGap("j2", "risk", "lint", 30.0),
        GateGap("j3", "crawl", "risk", 100.0),
    ]
    rows = summarize_gaps(gaps)
    # sorted by avg desc: crawl->risk (100) before risk->lint (20)
    assert rows[0]["transition"] == "crawl->risk"
    assert rows[1]["transition"] == "risk->lint"
    assert rows[1]["count"] == 2
    assert rows[1]["avg_seconds"] == 20.0
    assert rows[1]["max_seconds"] == 30.0
    # job_id must not leak into the rolled-up rows
    assert all("job_id" not in r for r in rows)


def test_iter_events_skips_torn_trailing_line(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(ts="2026-06-16T00:00:00Z", stage="risk", event=EVENT_RISK_GATE,
               job_id="j1", actor="m", extra={"status": "pass"})
    # simulate a concurrent pre-fsync partial write of a second record
    with (tmp_path / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"seq":1,"ts":"2026-06-16T00:00:01Z","eve')  # torn, no newline
    events = log.iter_events()
    assert len(events) == 1  # torn line skipped, valid record survives
    assert events[0]["event"] == EVENT_RISK_GATE
    # and aggregation still works end-to-end
    s = aggregate_audit(events)
    assert {g.gate for g in s.gates} == {EVENT_RISK_GATE}
