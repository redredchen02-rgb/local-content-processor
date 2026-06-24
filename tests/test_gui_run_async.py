"""Unit tests for gui.Api.run_until_draft_async and stage tracking (U2)."""

from __future__ import annotations

import threading
import time

import yaml

from lcp.gui import Api


def _api(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    return Api(config_path=str(cfg))


def _wait_settled(api, job_id, *, max_sec=15):
    """Poll job_status until status leaves 'running'."""
    for _ in range(int(max_sec * 10)):
        st = api.job_status(job_id)
        if st.get("status") not in ("running",):
            return st
        time.sleep(0.1)
    return api.job_status(job_id)


def test_run_until_draft_async_returns_running_immediately(tmp_path):
    api = _api(tmp_path)
    res = api.run_until_draft_async("j1", "https://example.com")
    # Immediate response still includes kind + stage alongside status/job_id.
    assert res["status"] == "running"
    assert res["job_id"] == "j1"
    assert res["kind"] == "run"


def test_run_until_draft_async_inflight_guard(tmp_path):
    """Second call while in-flight returns existing status without launching a second thread."""
    api = _api(tmp_path)
    first = api.run_until_draft_async("j-guard", "https://example.com")
    assert first["status"] == "running"
    second = api.run_until_draft_async("j-guard", "https://example.com")
    assert second["status"] == "running"
    # Exactly one inflight slot occupied — the guard blocked the second call.
    assert len(api.inflight) == 1


def test_run_until_draft_async_settles_without_hanging(tmp_path):
    """Background thread must settle to done/error — never stuck at 'running'."""
    api = _api(tmp_path)
    kick = api.run_until_draft_async("j-settle", "https://example.com")
    assert kick["status"] == "running"
    result = _wait_settled(api, "j-settle", max_sec=30)
    # Network may succeed or fail in CI — either way it must not stay "running".
    assert result.get("status") in ("done", "error", "idle", "unknown")


def test_run_until_draft_async_is_gui_only():
    """Verified via test_cli_gui_parity: run_until_draft_async must be in _GUI_ONLY."""
    from tests.test_cli_gui_parity import _GUI_ONLY

    assert "run_until_draft_async" in _GUI_ONLY


def test_run_until_draft_async_not_in_public_routes():
    """Private helper _do_run_until_draft must not appear as a public route."""
    from lcp.webserver import public_routes

    routes = public_routes(Api)
    # The async twin IS public (it is an operator action).
    assert "run_until_draft_async" in routes
    # The private sync helper is not a route.
    assert "_do_run_until_draft" not in routes


# ---------------------------------------------------------------------------
# U2 stage-tracking: kind + stage in job_status
# ---------------------------------------------------------------------------


def test_create_and_crawl_async_status_has_crawl_kind_and_stage(tmp_path):
    """create_and_crawl_async sets kind='crawl' and stage='crawl' immediately."""
    api = _api(tmp_path)
    res = api.create_and_crawl_async("jc", "https://example.com")
    assert res["status"] == "running"
    assert res["kind"] == "crawl"
    assert res["stage"] == "crawl"


def test_process_async_status_has_process_kind(tmp_path):
    """process_async with dry_run=False sets kind='process' immediately."""
    import yaml as _yaml

    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    store = JobStore(base_dir=tmp_path)
    store.create_job("jp", created_at="2026-06-24T00:00:00Z")
    store.set_state("jp", JobState.CRAWLED, updated_at="2026-06-24T00:00:00Z")
    (tmp_path / "jp" / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jp" / "raw" / "source.txt").write_text("test source", encoding="utf-8")

    api = Api(config_path=str(cfg))
    res = api.process_async("jp", title="test")
    assert res["status"] == "running"
    assert res["kind"] == "process"
    assert res["stage"] is None  # stage updated by on_stage callback during execution


def test_process_async_dry_run_sets_process_dry_kind(tmp_path):
    """process_async with dry_run=True sets kind='process_dry' immediately."""
    import yaml as _yaml

    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    store = JobStore(base_dir=tmp_path)
    store.create_job("jpd", created_at="2026-06-24T00:00:00Z")
    store.set_state("jpd", JobState.CRAWLED, updated_at="2026-06-24T00:00:00Z")
    (tmp_path / "jpd" / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jpd" / "raw" / "source.txt").write_text("test source", encoding="utf-8")

    api = Api(config_path=str(cfg))
    res = api.process_async("jpd", dry_run=True)
    assert res["kind"] == "process_dry"


def test_process_async_on_stage_updates_stage_via_blocking_gate(tmp_path, monkeypatch):
    """on_stage closure writes gate name into _status[job_id]['stage'] mid-flight.

    Uses a threading.Event to hold the gate chain at the 'risk' gate so we can
    read the stage before the background thread progresses further."""
    import yaml as _yaml

    from lcp.adapters.processor import gate_registry
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_yaml.safe_dump({"storage": {"base_dir": str(tmp_path)}}), encoding="utf-8")
    store = JobStore(base_dir=tmp_path)
    store.create_job("jstage", created_at="2026-06-24T00:00:00Z")
    store.set_state("jstage", JobState.CRAWLED, updated_at="2026-06-24T00:00:00Z")
    (tmp_path / "jstage" / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jstage" / "raw" / "source.txt").write_text("test source", encoding="utf-8")

    stage_seen = threading.Event()
    gate_released = threading.Event()

    original_run = gate_registry.run_gate_chain

    def blocking_run(gates, ctx, *, on_stage=None):
        if on_stage is not None:
            on_stage("risk")   # fire the first callback signal
            stage_seen.set()   # tell the main thread we fired
            gate_released.wait(timeout=5)  # hold until test releases us
        return original_run(gates, ctx, on_stage=None)  # run without further callbacks

    monkeypatch.setattr(gate_registry, "run_gate_chain", blocking_run)

    api = Api(config_path=str(cfg))
    api.process_async("jstage", title="t")

    # Wait for on_stage("risk") to fire, then check the status dict.
    assert stage_seen.wait(timeout=5), "on_stage callback never fired"
    st = api.job_status("jstage")
    assert st["status"] == "running"
    assert st["stage"] == "risk"
    assert st["kind"] == "process"

    gate_released.set()  # unblock the background thread
