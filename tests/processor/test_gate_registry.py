"""Tests for run_gate_chain on_stage callback (plan: realtime-job-progress U1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lcp.adapters.processor.gate_registry import GateContext, GateSpec, run_gate_chain
from lcp.core.state import JobState


def _ctx():
    """Minimal GateContext using MagicMock for store/audit (unused by mock gates)."""
    return GateContext(
        job_id="j",
        store=MagicMock(),
        audit=MagicMock(),
        ts="2026-01-01T00:00:00Z",
        title="t",
        source_text="s",
    )


def _pass_gate(name: str) -> GateSpec:
    return GateSpec(name, lambda ctx: None)


def _park_gate(name: str, state: JobState = JobState.BLOCKED) -> GateSpec:
    return GateSpec(name, lambda ctx: state)


# ---------------------------------------------------------------------------
# Happy path: all 3 gates pass → callback called in order
# ---------------------------------------------------------------------------


def test_callback_called_in_gate_order_when_all_pass():
    gates = [_pass_gate("risk"), _pass_gate("media"), _pass_gate("dedup")]
    calls: list[str] = []
    parked, stopped = run_gate_chain(gates, _ctx(), on_stage=calls.append)
    assert parked is None
    assert stopped is None
    assert calls == ["risk", "media", "dedup"]


# ---------------------------------------------------------------------------
# Early stop: first gate parks → only that gate's callback fires
# ---------------------------------------------------------------------------


def test_callback_stops_when_first_gate_parks():
    gates = [_park_gate("risk"), _pass_gate("media"), _pass_gate("dedup")]
    calls: list[str] = []
    parked, stopped = run_gate_chain(gates, _ctx(), on_stage=calls.append)
    assert parked is JobState.BLOCKED
    assert stopped == "risk"
    assert calls == ["risk"]  # "media" and "dedup" never fire


def test_callback_stops_at_middle_gate():
    gates = [_pass_gate("risk"), _park_gate("media"), _pass_gate("dedup")]
    calls: list[str] = []
    run_gate_chain(gates, _ctx(), on_stage=calls.append)
    assert calls == ["risk", "media"]


# ---------------------------------------------------------------------------
# on_stage=None: no callback, no AttributeError
# ---------------------------------------------------------------------------


def test_no_callback_is_safe():
    gates = [_pass_gate("risk"), _pass_gate("media")]
    parked, stopped = run_gate_chain(gates, _ctx(), on_stage=None)
    assert parked is None
    assert stopped is None


def test_no_callback_default_kwarg():
    """run_gate_chain with only positional args (existing callers) still works."""
    gates = [_pass_gate("risk")]
    parked, stopped = run_gate_chain(gates, _ctx())
    assert parked is None


# ---------------------------------------------------------------------------
# Callback raises: exception swallowed, gate execution continues
# ---------------------------------------------------------------------------


def test_callback_exception_is_swallowed_and_gate_continues():
    def _boom(name: str) -> None:
        raise RuntimeError("UI exploded")

    gates = [_pass_gate("risk"), _pass_gate("media")]
    # Must not raise; both gates still run and both pass.
    parked, stopped = run_gate_chain(gates, _ctx(), on_stage=_boom)
    assert parked is None
    assert stopped is None


def test_callback_exception_does_not_prevent_gate_from_parking():
    def _boom(name: str) -> None:
        raise ValueError("bad callback")

    gates = [_park_gate("risk")]
    parked, stopped = run_gate_chain(gates, _ctx(), on_stage=_boom)
    assert parked is JobState.BLOCKED
    assert stopped == "risk"
