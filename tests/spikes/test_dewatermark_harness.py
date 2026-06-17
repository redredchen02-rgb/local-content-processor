"""Harness-validation for the Unit 6 de-watermark spike.

NOT a feature test — it guards the spike from silently rotting. It loads the
spike's run_eval, runs the baseline engine over the SYNTHETIC stratified set, and
asserts (a) the engine runs end to end, (b) the metric structure is well-shaped,
(c) main() exits 0 and emits JSON. The real BUILD/CUT decision is out of scope —
that needs the operator's owned samples on the target laptop (latency).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SPIKE = Path(__file__).resolve().parents[1].parent / "spikes" / "dewatermark" / "run_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("dewm_run_eval", _SPIKE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dewm_run_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_synthesize_is_stratified():
    m = _load()
    samples = m.synthesize_samples(per_bucket=3)
    assert len(samples) == 3 * len(m.BUCKETS)
    assert {s.bucket for s in samples} == set(m.BUCKETS)


def test_baseline_engine_runs_and_scores():
    m = _load()
    samples = m.synthesize_samples(per_bucket=2)
    report = m.build_report(samples, m.NeighbourFillEngine())
    assert report["engine"] == "pillow_neighbourfill"
    assert report["sample_count"] == 2 * len(m.BUCKETS)
    assert {r["bucket"] for r in report["per_bucket"]} == set(m.BUCKETS)
    for r in report["per_bucket"]:
        assert 0.0 <= r["publishable_rate"] <= 1.0
        assert r["mean_latency_ms"] >= 0.0
        assert r["verdict"] in {"PASS", "BELOW BAR", "out-of-scope v1 (not gated)"}
    assert report["go_no_go"] in {"GO", "NO-GO/REVIEW"}


def test_out_of_scope_bucket_d_not_gated():
    m = _load()
    report = m.build_report(m.synthesize_samples(per_bucket=2), m.NeighbourFillEngine())
    d = next(r for r in report["per_bucket"] if r["bucket"] == "d")
    assert d["bar"] is None
    assert d["verdict"] == "out-of-scope v1 (not gated)"


def test_psnr_identical_is_inf():
    m = _load()
    from PIL import Image
    img = Image.new("RGB", (8, 8), (10, 20, 30))
    assert m._psnr(img, img) == float("inf")


def test_main_json_exits_zero(capsys):
    m = _load()
    rc = m.main(["--json", "--per-bucket", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["engine"] == "pillow_neighbourfill"
    assert "per_bucket" in out
