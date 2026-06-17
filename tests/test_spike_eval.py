"""Harness-validation for the Unit 1 detection-accuracy spike.

This is NOT a feature test — it guards the spike from silently rotting. It loads
the spike's run_eval, runs it on the bundled SYNTHETIC sample set, and asserts it
(a) imports the real detectors and runs end to end, (b) returns a well-shaped
metrics structure, and (c) exits 0 via its main(). The actual accuracy DECISION
(substring-only vs +NLI) is out of scope — that needs a real labeled corpus.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPIKE_DIR = Path(__file__).resolve().parents[1] / "spikes" / "detection_accuracy"
_RUN_EVAL = _SPIKE_DIR / "run_eval.py"
_SAMPLE = _SPIKE_DIR / "sample_labeled.jsonl"


def _load_run_eval():
    spec = importlib.util.spec_from_file_location("spike_run_eval", _RUN_EVAL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["spike_run_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_eval():
    return _load_run_eval()


def test_spike_files_exist():
    assert _RUN_EVAL.exists(), "run_eval.py missing"
    assert _SAMPLE.exists(), "sample_labeled.jsonl missing"
    assert (_SPIKE_DIR / "README.md").exists(), "README.md missing"


def test_sample_set_loads_and_is_nontrivial(run_eval):
    rows = run_eval.load_labeled(_SAMPLE)
    assert len(rows) >= 20, "sample set should have ~20-30 rows"
    kinds = {r["kind"] for r in rows}
    # All three detectors must be represented.
    assert {"grounding", "risk", "dedup"} <= kinds


def test_build_report_has_expected_shape(run_eval):
    rows = run_eval.load_labeled(_SAMPLE)
    report = run_eval.build_report(rows)

    assert report["sample_count"] == len(rows)
    for detector in ("grounding", "risk", "dedup"):
        assert detector in report, f"missing detector block: {detector}"
        assert report[detector], f"{detector} has no strategies scored"

    # The substring baseline must be present (it's the zero-dep default).
    assert "substring_overlap_0.6" in report["grounding"]

    # Every metrics block has the full set of fields with sane ranges.
    for detector in ("grounding", "risk", "dedup"):
        for strategy, m in report[detector].items():
            for field in (
                "tp", "fp", "tn", "fn", "total",
                "precision", "recall",
                "false_positive_rate", "false_negative_rate", "accuracy",
            ):
                assert field in m, f"{detector}/{strategy} missing {field}"
            assert m["total"] == m["tp"] + m["fp"] + m["tn"] + m["fn"]
            assert 0.0 <= m["precision"] <= 1.0
            assert 0.0 <= m["recall"] <= 1.0


def test_real_detectors_are_exercised(run_eval):
    """Sanity: the detectors actually discriminate (not all-pass / all-fail), so
    the metrics reflect real detector behavior, not a stub."""
    rows = run_eval.load_labeled(_SAMPLE)
    report = run_eval.build_report(rows)

    risk = report["risk"]["flag_any"]
    # The synthetic risk set has both clean and flagged rows -> both classes hit.
    assert risk["tp"] > 0 and risk["tn"] > 0

    dedup = report["dedup"]["not_unique"]
    assert dedup["tp"] > 0 and dedup["tn"] > 0


def test_main_runs_and_exits_zero(run_eval, capsys):
    rc = run_eval.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "detection accuracy" in out.lower()


def test_main_json_mode_exits_zero(run_eval, capsys):
    rc = run_eval.main(["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"grounding"' in out


def test_main_missing_file_returns_error(run_eval, tmp_path):
    rc = run_eval.main(["--labeled", str(tmp_path / "does-not-exist.jsonl")])
    assert rc == 2
