"""Unit 5: actionable NEEDS_REVISION guidance + the dry-run "no packet" advisory.

The operator should learn WHY a run did not reach a packet, in plain terms, and
WHICH sections are missing — never a bare "needs_revision".
"""

from __future__ import annotations

import pytest

from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.cli import _completion_advisory as cli_advisory
from lcp.core.config import Config, PublisherConfig
from lcp.core.state import JobState
from lcp.gui import _completion_advisory as gui_advisory
from tests.support.pipeline_fakes import (
    TITLE,
    build_pipeline,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-18T00:00:00Z"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


def test_dry_run_advisory_explains_no_packet():
    msg = cli_advisory(JobState.NEEDS_REVISION, dry_run=True)
    assert msg is not None
    assert "dry-run" in msg.lower() and "PROCESSED" in msg


def test_needs_revision_advisory_points_to_ai_copy():
    msg = cli_advisory(JobState.NEEDS_REVISION, dry_run=False)
    assert msg is not None and "ai-copy" in msg.lower()


def test_no_advisory_when_processed_or_review():
    assert cli_advisory(JobState.PROCESSED, dry_run=False) is None
    assert cli_advisory(JobState.REVIEW_PENDING, dry_run=False) is None


def test_cli_and_gui_advisory_are_identical():
    # CLI/GUI parity: the same operator copy on both shells.
    for state in (JobState.NEEDS_REVISION, JobState.PROCESSED):
        for dry in (True, False):
            assert cli_advisory(state, dry_run=dry) == gui_advisory(state, dry_run=dry)


def test_process_surfaces_missing_section_reasons_in_notes(store, audit):
    # Without --ai-copy the copywriter sections stay empty, so lint parks the job
    # AND the notes name the missing canonical sections (PII-free labels).
    config = Config(publisher=PublisherConfig())
    seed_clean_index(store)
    p = build_pipeline(store, audit, config=config)
    p.stage1(spec_for(store, "g1"), ts=TS)
    res = p.process("g1", ts=TS, title=TITLE, ai_copy=False)
    assert res.final_state is JobState.NEEDS_REVISION
    assert any("missing required section" in n for n in res.notes), res.notes
