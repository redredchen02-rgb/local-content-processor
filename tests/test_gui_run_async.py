"""Unit tests for gui.Api.run_until_draft_async (U2: one-shot Stage1+Stage2)."""

from __future__ import annotations

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
    assert res == {"job_id": "j1", "status": "running"}


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
