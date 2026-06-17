"""Dedup gate orchestration (imperative shell).

Loads the local job index / published-site index off disk, hands an in-memory
:class:`~lcp.core.rules.dedup_rules.DedupIndex` to the pure cascade, then maps
the :class:`DedupResult` onto a :class:`~lcp.core.state.JobState`:
  * duplicate          -> DUPLICATE (its own terminal state, distinct from BLOCKED)
  * uncertain          -> NEEDS_HUMAN_REVIEW + ReviewReason.DEDUP
  * unique             -> caller continues (no state write here)

HONESTY (R36): the *site/published index* is the one whose absence makes a
``unique`` verdict untrustworthy. When that file is missing we load whatever
local jobs we have but flag ``site_index_available=False`` so the pure layer
downgrades ``unique`` -> ``uncertain`` and emits a reliability warning. We NEVER
auto-reject — ``duplicate`` is advisory; the human is the gate.

The local index file is a tiny JSONL of PII-light entries (job_id + title +
body) the operator/pipeline maintains. We deliberately keep parsing trivial and
NEVER fetch a URL (plan: linter/dedup MUST NOT parse/resolve URLs)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...core.rules import dedup_rules
from ...core.rules.dedup_rules import (
    DedupIndex,
    DedupQuery,
    DedupResult,
    DedupStatus,
    IndexEntry,
)
from ...core.state import JobState, ReviewReason
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from ._persist import persist_gate_state

EVENT_DEDUP_GATE = "DEDUP_GATE"

# Default name of the local published/site index file (JSONL). Its ABSENCE is
# what triggers fail-loud LOW reliability.
SITE_INDEX_FILENAME = "site_index.jsonl"


@dataclass(frozen=True)
class DedupGateOutcome:
    result: DedupResult
    job_state: JobState | None  # None when status==unique (caller continues)
    review_reason: ReviewReason | None = None


@dataclass(frozen=True)
class DedupScoreParams:
    """Thresholds forwarded to the pure dedup cascade (calibration deferred).

    An explicit, typed replacement for the prior ``**score_params: Any`` seam at
    this strict-adapter boundary; defaults mirror dedup_rules.assess_dedup."""

    duplicate_jaccard: float = dedup_rules.DEFAULT_DUPLICATE_JACCARD
    uncertain_jaccard: float = dedup_rules.DEFAULT_UNCERTAIN_JACCARD
    lsh_threshold: float = dedup_rules.DEFAULT_LSH_THRESHOLD
    num_perm: int = dedup_rules.DEFAULT_NUM_PERM
    k: int = dedup_rules.DEFAULT_SHINGLE_K


def load_site_index(path: str | Path) -> DedupIndex:
    """Load a published/site index JSONL into a :class:`DedupIndex`.

    Each line: {"job_id": "...", "title": "...", "body": "..."}. A MISSING file
    -> site_index_available=False (fail-loud, R36): we honestly report we have
    no trustworthy index to confirm uniqueness. An EMPTY existing file still
    counts as available (the operator asserts it is the real, current index)."""
    p = Path(path)
    if not p.exists():
        return DedupIndex(entries=(), site_index_available=False)
    entries: list[IndexEntry] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        entries.append(
            IndexEntry(
                job_id=str(obj["job_id"]),
                title=str(obj.get("title", "")),
                body=str(obj.get("body", "")),
            )
        )
    return DedupIndex(entries=tuple(entries), site_index_available=True)


def _map_to_state(result: DedupResult) -> tuple[JobState | None, ReviewReason | None]:
    if result.status == DedupStatus.DUPLICATE:
        return JobState.DUPLICATE, None
    if result.status == DedupStatus.UNCERTAIN:
        return JobState.NEEDS_HUMAN_REVIEW, ReviewReason.DEDUP
    return None, None  # UNIQUE -> caller continues


def run_dedup_gate(
    *,
    job_id: str,
    title: str,
    body: str,
    store: JobStore,
    audit: AuditLog,
    ts: str,
    site_index_path: str | Path | None = None,
    queries: list[DedupQuery] | None = None,
    actor: str = "system",
    score_params: DedupScoreParams | None = None,
) -> DedupGateOutcome:
    """Run the dedup gate: load the index, score, map, audit, persist.

    `site_index_path` points at the published/site index JSONL. If None or
    missing, the gate runs fail-loud (LOW reliability, never confident unique,
    never auto-reject). `score_params` carries the pure-cascade thresholds
    (calibration deferred); None uses the defaults."""
    if site_index_path is None:
        site_index_path = store.base_dir / SITE_INDEX_FILENAME
    index = load_site_index(site_index_path)

    sp = score_params or DedupScoreParams()
    result = dedup_rules.assess_dedup(
        title=title,
        body=body,
        index=index,
        queries=queries or [],
        duplicate_jaccard=sp.duplicate_jaccard,
        uncertain_jaccard=sp.uncertain_jaccard,
        lsh_threshold=sp.lsh_threshold,
        num_perm=sp.num_perm,
        k=sp.k,
    )
    job_state, review_reason = _map_to_state(result)

    audit.append(
        ts=ts,
        stage="dedup",
        event=EVENT_DEDUP_GATE,
        job_id=job_id,
        actor=actor,
        extra={
            "status": result.status.value,
            "reliability": result.reliability.value,
            "matched_job_ids": [m.job_id for m in result.matched_items],
            "review_reason": review_reason.value if review_reason else None,
            "warning_count": len(result.warnings),
        },
    )

    if job_state is not None:
        persist_gate_state(
            store,
            job_id,
            job_state,
            updated_at=ts,
            review_reason=review_reason,
        )
    return DedupGateOutcome(
        result=result, job_state=job_state, review_reason=review_reason
    )
