"""Declarative Stage-2 gate registry (plan-003 U1).

Replaces the hand-written copy-paste park-or-pass sequence (risk/media/dedup)
with an ordered ``GateSpec`` list. Adding a new uniform gate is one list entry
+ one checker — no new ``if/return`` block in ``_process_inner``.

The registry covers only the **uniform park-or-pass gates** (risk, media, dedup).
Assemble + lint+grounding stay explicit post-registry calls because they produce
/enrich a ``Draft`` and consume cross-gate reports (``media.report`` for
``has_images``).

Fail-closed order is DATA: the list order IS the gate order. The runner stops
at the first gate that returns a non-None ``JobState`` and derives ``stopped_at``
from the gate's name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...core.config import MediaConfig, WatermarkConfig
from ...core.rules.risk_rules import RiskInput
from ...core.state import JobState
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore
from . import dedup_checker, media_checker, risk_checker


@dataclass(frozen=True)
class GateSpec:
    """A named gate callable. ``run(ctx) -> JobState | None``.

    ``None`` means the gate passed (caller continues). A ``JobState`` means the
    job is parked and the runner stops."""

    name: str
    run: Callable[["GateContext"], JobState | None]


@dataclass
class GateContext:
    """Mutable context carried through the gate chain.

    Gates may stash reports here (e.g. ``media.report``) so downstream gates
    (lint) can read them. The context is created once per ``_process_inner``
    call and does not outlive it."""

    job_id: str
    store: JobStore
    audit: AuditLog
    ts: str
    title: str
    source_text: str
    # Optional overrides carried from Pipeline._process_inner.
    risk_input: RiskInput | None = None
    site_index_path: str | Path | None = None
    watermark_enabled: bool | None = None
    media_config: MediaConfig | None = None
    watermark_config: WatermarkConfig | None = None
    # Reports stashed by gates for downstream consumption (e.g. media -> lint).
    reports: dict[str, Any] = field(default_factory=dict)


# --- Gate wrapper functions (adapt individual gate signatures to GateContext) ---


def _run_risk_gate(ctx: GateContext) -> JobState | None:
    ri = ctx.risk_input or RiskInput(title=ctx.title, body=ctx.source_text)
    outcome = risk_checker.run_risk_gate(
        job_id=ctx.job_id,
        content=ri,
        store=ctx.store,
        audit=ctx.audit,
        ts=ctx.ts,
    )
    return outcome.job_state


def _run_media_gate(ctx: GateContext) -> JobState | None:
    wm = ctx.watermark_config
    if ctx.watermark_enabled is not None and wm is not None:
        wm = wm.model_copy(update={"enabled": ctx.watermark_enabled})
    if ctx.media_config is None:
        return None  # no media config = no-op gate (pass)
    outcome = media_checker.run_media_gate(
        job_id=ctx.job_id,
        store=ctx.store,
        audit=ctx.audit,
        ts=ctx.ts,
        media_config=ctx.media_config,
        watermark=wm,
    )
    ctx.reports["media"] = outcome.report
    return outcome.job_state


def _run_dedup_gate(ctx: GateContext) -> JobState | None:
    outcome = dedup_checker.run_dedup_gate(
        job_id=ctx.job_id,
        title=ctx.title,
        body=ctx.source_text,
        store=ctx.store,
        audit=ctx.audit,
        ts=ctx.ts,
        site_index_path=ctx.site_index_path,
    )
    return outcome.job_state


# --- The default gate chain (fail-closed order is DATA) ---

PARK_GATES: list[GateSpec] = [
    GateSpec("risk", _run_risk_gate),
    GateSpec("media", _run_media_gate),
    GateSpec("dedup", _run_dedup_gate),
]


def run_gate_chain(
    gates: list[GateSpec],
    ctx: GateContext,
) -> tuple[JobState | None, str | None]:
    """Run the ordered gate chain. Returns (parked_state, stopped_at_name).

    ``(None, None)`` means all gates passed. ``(state, name)`` means the gate
    ``name`` parked the job at ``state``."""
    for gate in gates:
        state = gate.run(ctx)
        if state is not None:
            return state, gate.name
    return None, None
