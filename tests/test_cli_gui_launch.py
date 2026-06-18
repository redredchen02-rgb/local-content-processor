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
