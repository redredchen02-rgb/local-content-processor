"""U6 — E2E risk redline: source with redline content parks at BLOCKED.

Drives a source containing unambiguous redline content (minor / NCII) through
the real Stage-2 gate chain. The risk gate must hard-stop before any media or
LLM work is done, and the final state must be BLOCKED with the correct
RiskCategory codes recorded in the audit trail.
"""

from __future__ import annotations

import pytest

from lcp.adapters.processor.risk_checker import EVENT_RISK_GATE
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    TITLE,
    build_pipeline,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-22T00:00:00Z"

# Redline content (minor / NCII) — must hard-stop at the risk gate.
REDLINE = "該網站內容涉及未成年色情與兒童不雅影像。\n此為違法內容請勿散佈。\n"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


@pytest.fixture()
def config():
    return Config()


def test_redline_source_parks_at_blocked(store, audit, config):
    """Unambiguous redline content → BLOCKED with risk gate stop."""
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config, source=REDLINE)
    p.stage1(spec_for(store, "rl1"), ts=TS)
    res = p.process("rl1", ts=TS, title=TITLE, ai_copy=True)

    assert res.final_state is JobState.BLOCKED, (
        f"expected BLOCKED, got {res.final_state}: {res.notes}"
    )
    assert res.stopped_at == "risk", f"stopped at {res.stopped_at}, expected risk"

    # Audit trail records the gate decision with risk categories.
    lines = audit._read_lines()
    gate_events = [l for l in lines if l["event"] == EVENT_RISK_GATE]
    assert gate_events, "no RISK_GATE audit event found"
    last = gate_events[-1]
    assert "flag_categories" in last.get("extra", {}), "missing flag_categories in risk gate event"


def test_clean_source_not_blocked(store, audit, config):
    """Ordinary non-redline content should NOT be blocked by the risk gate."""
    seed_clean_index(store)
    CLEAN = "台北市政府今日宣布將舉辦夏日音樂祭。\n活動免費入場歡迎民眾踴躍參加。\n"

    p = build_pipeline(store, audit, config=config, source=CLEAN)
    p.stage1(spec_for(store, "cl1"), ts=TS)
    TITLE2 = "台北市政府今日宣布將舉辦夏日音樂祭免費入場歡迎參加"
    res = p.process("cl1", ts=TS, title=TITLE2, ai_copy=True)

    # Should NOT be BLOCKED — it may fail later gates (no LLM key etc.), but
    # the risk gate must pass it through.
    assert res.final_state is not JobState.BLOCKED, f"clean source blocked: {res.notes}"
