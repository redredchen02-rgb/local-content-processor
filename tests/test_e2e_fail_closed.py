"""Unit 3: fail-closed variants driven through the real Stage-2 chain.

Each foreseeable bad input must PARK at the correct hold state (never silently
pass), and a foreseeable bad input at the CLI must be a typed exit (2/3/4),
NEVER the unexpected-error exit 5 (docs/solutions/fail-closed-catch-at-gate-
boundary.md). Guards the narrow `except ExternalServiceError` boundary against
being widened to swallow a crash.
"""

from __future__ import annotations

import pytest

from lcp.adapters.llm.client import ChatResult
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.cli import main
from lcp.core.errors import EXIT_INPUT, EXIT_INTERNAL, ExternalServiceError
from lcp.core.state import JobState
from tests.support.pipeline_fakes import (
    BODY,
    SOURCE,
    TITLE,
    DualModeChatClient,
    build_pipeline,
    seed_clean_index,
    spec_for,
)

TS = "2026-06-18T00:00:00Z"
# Confident redline content (minor / NCII) — must hard-stop at the risk gate.
REDLINE_SOURCE = "本則內容涉及未成年色情與兒童不雅影像，屬於未成年色情內容。"


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


def _run(store, audit, *, client=None, source=SOURCE, job="fc"):
    seed_clean_index(store)
    p = build_pipeline(store, audit, llm_client=client, source=source)
    p.stage1(spec_for(store, job), ts=TS)
    return p.process(job, ts=TS, title=TITLE, ai_copy=True)


def test_redline_source_parks_at_risk_gate(store, audit):
    res = _run(store, audit, source=REDLINE_SOURCE)
    assert res.stopped_at == "risk"
    # A redline hard-stops (BLOCKED); an ambiguous-redline downgrades to human
    # review. Either is fail-closed — it never proceeds to the LLM.
    assert res.final_state in (JobState.BLOCKED, JobState.NEEDS_HUMAN_REVIEW)


def test_ungrounded_generated_quick_fact_routes_to_human(store, audit):
    # The copywriter emits a quick_fact absent from the source — grounding (now
    # covering quick_facts, Unit 1) must park it, not silently pass.
    client = DualModeChatClient(
        body=BODY,
        copy="QUICKFACT: 太空人本週成功登陸火星並建立永久科研基地\n",
    )
    res = _run(store, audit, client=client)
    assert res.final_state is JobState.NEEDS_HUMAN_REVIEW


def test_truncated_draft_needs_revision(store, audit):
    class _Truncated:
        model = "fake-model"

        def chat(self, **kwargs):
            return ChatResult(
                text="partial...", finish_reason="length", model="fake-model",
                needs_revision=True, revision_reason="truncated:length",
                executed=True,
            )

    res = _run(store, audit, client=_Truncated())
    assert res.final_state is JobState.NEEDS_REVISION
    assert res.stopped_at == "assemble"


def test_llm_external_error_lands_process_failed(store, audit):
    class _Raising:
        model = "fake-model"

        def chat(self, **kwargs):
            raise ExternalServiceError("LLM call failed (503)")

    res = _run(store, audit, client=_Raising())
    assert res.final_state is JobState.PROCESS_FAILED
    assert not store.is_processing("fc")  # marker cleared on the error path


def test_cli_bad_input_is_typed_exit_never_internal(tmp_path):
    # Processing a non-existent job is a foreseeable bad input -> a typed
    # InputValidationError (exit 2), NEVER the unexpected-error exit 5.
    rc = main([
        "--output-dir", str(tmp_path),
        "process", "--job-id", "does-not-exist", "--title", TITLE,
    ])
    assert rc == EXIT_INPUT
    assert rc != EXIT_INTERNAL
