"""Unit 4: `lcp gui` wires to the webui server (no pywebview).

`serve()` blocks, so we monkeypatch it and assert the WIRING — that `gui` calls
`webserver.serve` with the resolved config path and the right flags — exactly as
the old test monkeypatched `webview.start`.
"""

import lcp.webserver as webserver
from lcp.cli import main
from lcp.core.errors import EXIT_OK, EXIT_USAGE


def _capture_serve(monkeypatch):
    captured: dict = {}

    def fake_serve(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(webserver, "serve", fake_serve)
    return captured


def test_gui_invokes_serve_with_config_and_defaults(monkeypatch, tmp_path):
    captured = _capture_serve(monkeypatch)
    cfg = str(tmp_path / "config.yaml")
    rc = main(["--config", cfg, "gui"])
    assert rc == EXIT_OK
    assert captured["config_path"] == cfg
    assert captured["port"] == webserver.DEFAULT_PORT
    assert captured["open_browser"] is True


def test_gui_forwards_port_and_no_browser(monkeypatch):
    captured = _capture_serve(monkeypatch)
    rc = main(["gui", "--port", "9001", "--no-browser"])
    assert rc == EXIT_OK
    assert captured["port"] == 9001
    assert captured["open_browser"] is False


def test_gui_has_no_host_option(monkeypatch):
    # No --host: the bind cannot be moved off loopback from the CLI.
    _capture_serve(monkeypatch)  # guard: serve must never actually run
    rc = main(["gui", "--host", "0.0.0.0"])
    assert rc == EXIT_USAGE


def test_make_api_resolves_none_to_config_yaml():
    # serve() must build the Api with a CONCRETE path so the Settings panel writes
    # and the rest reads the SAME config.yaml (the old launch() did this).
    assert webserver._make_api(None)._config_path == "config.yaml"
    assert webserver._make_api("/some/where/c.yaml")._config_path == "/some/where/c.yaml"


def test_settings_round_trip_through_resolved_config(tmp_path, monkeypatch):
    # Regression: `lcp gui` (no --config) saves base_url/model and they MUST take
    # effect. Before the fix, save_settings wrote config.yaml but get_settings (and
    # the readiness check) loaded defaults (config_path=None) — so the GUI stayed
    # stuck at "模型 endpoint 缺" forever despite "已储存".
    monkeypatch.chdir(tmp_path)
    api = webserver._make_api(None)  # -> Api(config_path="config.yaml")
    saved = api.save_settings("https://example.com/v1", "my-model", "")
    assert "error" not in saved, saved
    out = api.get_settings()
    assert out["base_url"] == "https://example.com/v1"  # read back, not default
    assert out["model"] == "my-model"
    assert (tmp_path / "config.yaml").exists()
