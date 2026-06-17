"""Unit 5: GUI surface for watermark / template / cover advisory + CLI parity."""

from __future__ import annotations

from pathlib import Path

import yaml

from lcp.gui import Api

APP_JS = Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "app.js"


def _api(tmp_path, *, templates=None):
    base = str(tmp_path)
    cfg = tmp_path / "config.yaml"
    body = {"storage": {"base_dir": base}}
    if templates:
        body["templates"] = templates
    cfg.write_text(yaml.safe_dump(body), encoding="utf-8")
    return Api(config_path=str(cfg)), base


def _crawled_job(base, job_id="jg"):
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    ts = "2026-06-17T00:00:00Z"
    store = JobStore(base_dir=base)
    store.create_job(job_id, created_at=ts)
    store.set_state(job_id, JobState.CRAWLED, updated_at=ts)
    raw = store.job_dir(job_id) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "source.txt").write_text("某活动周末举办。", encoding="utf-8")
    return store


def test_templates_lists_configured_categories(tmp_path):
    api, _ = _api(tmp_path, templates={"网红黑料": "为 {category} 写作", "海外吃瓜": "{category}"})
    res = api.templates()
    assert res["categories"] == ["海外吃瓜", "网红黑料"]


def test_templates_empty_when_none_configured(tmp_path):
    api, _ = _api(tmp_path)
    assert api.templates()["categories"] == []


def test_process_accepts_batch1_inputs_dry_run(tmp_path):
    api, base = _api(tmp_path, templates={"网红黑料": "为 {category} 写作"})
    _crawled_job(base, "jg")
    res = api.process("jg", "", True, True, "网红黑料", True)
    assert "error" not in res
    assert res["dry_run"] is True


def test_cover_report_no_report_is_graceful(tmp_path):
    api, base = _api(tmp_path)
    _crawled_job(base, "jg2")
    res = api.cover_report("jg2")
    assert res["has_report"] is False


# --- static parity / discipline checks ---------------------------------------


def test_app_js_wires_new_surface():
    src = APP_JS.read_text(encoding="utf-8")
    assert "templateSelect" in src
    assert "cover_report" in src
    # process_async is called with the process-time args (watermark is tri-state)
    assert "watermarkSelect" in src and "wmChoice" in src and "ai.checked" in src
    # render discipline: no innerHTML sink anywhere
    assert "innerHTML" not in src
