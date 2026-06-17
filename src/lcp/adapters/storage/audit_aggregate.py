"""Pure aggregation of audit events into dashboard metrics.

No I/O, no datetime.now(): a caller passes already-read events (see
``AuditLog.iter_events``) and gets back a plain, PII-free summary the GUI can
render. Keeping it pure means it is fully unit-testable headless, in line with
the project's "pure core + imperative shell" split.

What it computes:
- per-gate INTERCEPT rate, with an honest denominator = the number of distinct
  jobs that REACHED that gate (a job blocked early at RISK never reaches
  GROUNDING, so a shared "all jobs" denominator would understate late gates).
- review_reason counts (enum codes only — never free text).
- gate-to-gate ELAPSED gaps, grouped per job_id. NOTE: ts is operator-action
  time minted at the Api boundary, so a gap includes operator wait time, not
  compute cost. We surface it labelled "含等待" and deliberately keep it OUT of
  the optimization hints so it cannot misdirect (plan decision).
- daily throughput buckets (distinct jobs touched per calendar day).

Source identifiers (URLs/domains) are intentionally absent from audit
(``_PROHIBITED_KEYS``), so a "repeated source" signal is NOT computed here;
that belongs to the jobs sha256 columns / saved_sources, not audit."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# Gate event names. These MIRROR the EVENT_*_GATE constants defined next to the
# processor gates (risk_checker / dedup_checker / draft_linter / media_checker).
# We keep local copies rather than importing those modules so this lower-level
# storage helper does not pull the processors' heavy module-level imports and
# does not invert the adapter layering. test_audit_aggregate asserts they stay
# in sync, so drift is caught.
EVENT_RISK_GATE = "RISK_GATE"
EVENT_DEDUP_GATE = "DEDUP_GATE"
EVENT_LINT_GATE = "LINT_GATE"
EVENT_GROUNDING_GATE = "GROUNDING_GATE"
EVENT_MEDIA_GATE = "MEDIA_GATE"

# The status value(s) that mean "passed clean" for each gate. Anything else
# recorded for that gate counts as an intercept (hold / block / duplicate /
# needs-revision). Sourced from the gate status enums in core/rules/.
_GATE_PASS_STATUS: dict[str, frozenset[str]] = {
    EVENT_RISK_GATE: frozenset({"pass"}),
    EVENT_DEDUP_GATE: frozenset({"unique"}),
    EVENT_LINT_GATE: frozenset({"pass"}),
    EVENT_GROUNDING_GATE: frozenset({"pass"}),
    EVENT_MEDIA_GATE: frozenset({"pass"}),
}

_GATE_EVENTS: frozenset[str] = frozenset(_GATE_PASS_STATUS)


@dataclass(frozen=True)
class GateStat:
    """Intercept stats for one gate.

    ``reached`` is the distinct-job denominator; ``rate`` is None when no job
    reached the gate (avoids a misleading 0 vs a true "no data")."""

    gate: str
    reached: int
    intercepted: int

    @property
    def rate(self) -> float | None:
        return self.intercepted / self.reached if self.reached else None


@dataclass(frozen=True)
class GateGap:
    """One gate-to-gate elapsed gap within a single job. Seconds INCLUDE
    operator wait time (ts is action time, not compute time)."""

    job_id: str
    from_stage: str
    to_stage: str
    seconds: float


@dataclass(frozen=True)
class AuditSummary:
    gates: list[GateStat] = field(default_factory=list)
    review_reasons: dict[str, int] = field(default_factory=dict)
    gate_gaps: list[GateGap] = field(default_factory=list)
    daily_jobs: dict[str, int] = field(default_factory=dict)


def _ts_date(ts: str) -> str:
    """Calendar day of an ISO8601 UTC ts. String slice only — no datetime
    parse, no now(): an event's ts already starts 'YYYY-MM-DD'."""
    return ts[:10]


def _parse_ts_seconds(ts: str) -> float | None:
    """Epoch-ish seconds for diffing two ts within one job. Returns None if the
    ts is not a parseable ISO8601 instant (diff is then skipped, not fatal)."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def aggregate_audit(events: list[dict[str, object]]) -> AuditSummary:
    """Reduce raw audit events into a PII-free dashboard summary.

    Tolerant by design: events missing ``extra``/``ts``/``job_id`` or carrying
    an unknown event type are skipped, never raised on (the reader may also
    have already dropped a torn line)."""
    reached: dict[str, set[str]] = {g: set() for g in _GATE_EVENTS}
    intercepted: dict[str, set[str]] = {g: set() for g in _GATE_EVENTS}
    reasons: Counter[str] = Counter()
    daily: dict[str, set[str]] = {}
    # job_id -> list of (seq, stage, ts) for gate events, to diff consecutively.
    per_job: dict[str, list[tuple[int, str, str]]] = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        gate = ev.get("event")
        if gate not in _GATE_EVENTS:
            continue
        job_id = ev.get("job_id")
        ts = ev.get("ts")
        if not isinstance(job_id, str) or not isinstance(ts, str):
            continue
        extra = ev.get("extra")
        extra = extra if isinstance(extra, dict) else {}

        reached[gate].add(job_id)
        status = extra.get("status")
        if status not in _GATE_PASS_STATUS[gate]:
            intercepted[gate].add(job_id)

        reason = extra.get("review_reason")
        if isinstance(reason, str) and reason:
            reasons[reason] += 1

        daily.setdefault(_ts_date(ts), set()).add(job_id)

        seq = ev.get("seq")
        stage = ev.get("stage")
        if isinstance(seq, int) and isinstance(stage, str):
            per_job.setdefault(job_id, []).append((seq, stage, ts))

    gates = [
        GateStat(gate=g, reached=len(reached[g]), intercepted=len(intercepted[g]))
        for g in sorted(_GATE_EVENTS)
        if reached[g]
    ]

    gaps: list[GateGap] = []
    for job_id, rows in per_job.items():
        rows.sort()  # by seq, then stage/ts
        for (_, from_stage, from_ts), (_, to_stage, to_ts) in zip(rows, rows[1:]):
            a = _parse_ts_seconds(from_ts)
            b = _parse_ts_seconds(to_ts)
            if a is None or b is None:
                continue
            gaps.append(
                GateGap(
                    job_id=job_id,
                    from_stage=from_stage,
                    to_stage=to_stage,
                    seconds=max(0.0, b - a),
                )
            )

    return AuditSummary(
        gates=gates,
        review_reasons=dict(reasons),
        gate_gaps=gaps,
        daily_jobs={d: len(j) for d, j in sorted(daily.items())},
    )
