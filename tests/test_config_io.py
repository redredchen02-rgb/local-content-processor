"""Config I/O adapter: YAML loading + keyring/env api_key resolution.

These exercise the moved-out I/O (U1 of plan 004 kept core/config.py pure). The
api_key resolution must keep keyring-then-env order and never leak the secret in
its DependencyError message."""

import pytest

from lcp.adapters.storage.config_io import has_api_key, load_config, resolve_api_key
from lcp.core.config import Config
from lcp.core.errors import DependencyError, InputValidationError


def test_defaults_when_no_path():
    cfg = load_config(None)
    assert cfg.media.image_width == 800
    assert cfg.media.cover_width == 1300 and cfg.media.cover_height == 640
    assert cfg.publisher.require_human_approval is True


def test_load_valid_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "crawler:\n  allow_domains: ['a.com', 'b.com']\nmedia:\n  image_width: 640\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.crawler.allow_domains == ["a.com", "b.com"]
    assert cfg.media.image_width == 640


def test_missing_file_is_input_error(tmp_path):
    with pytest.raises(InputValidationError):
        load_config(tmp_path / "nope.yaml")


def test_bad_yaml_is_input_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("crawler: [unclosed\n", encoding="utf-8")
    with pytest.raises(InputValidationError):
        load_config(p)


def test_root_must_be_mapping(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(InputValidationError):
        load_config(p)


def test_api_key_missing_raises_dependency_error_without_secret(monkeypatch):
    monkeypatch.delenv("LCP_LLM_API_KEY", raising=False)
    # Force keyring to return nothing regardless of host backend.
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None, raising=False)
    with pytest.raises(DependencyError) as ei:
        resolve_api_key(Config())
    # Error message must not leak any secret.
    assert "REDACTED" not in str(ei.value)


def test_api_key_from_env_fallback(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None, raising=False)
    monkeypatch.setenv("LCP_LLM_API_KEY", "env-secret-123")
    assert resolve_api_key(Config()) == "env-secret-123"


def test_has_api_key_reflects_env_without_revealing(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None, raising=False)
    monkeypatch.delenv("LCP_LLM_API_KEY", raising=False)
    assert has_api_key(Config()) is False
    monkeypatch.setenv("LCP_LLM_API_KEY", "sk-secret")
    assert has_api_key(Config()) is True


def test_config_io_has_no_module_level_io():
    """Importing config_io must not read files/keyring/env at import time, so an
    early import can never run before apply_hardening() sets the umask."""
    import inspect

    import lcp.adapters.storage.config_io as cio

    src = inspect.getsource(cio)
    # Crude but effective: keyring/open/read calls only appear inside functions
    # (indented), never at column 0 (module scope).
    for marker in ("keyring.get_password(", "keyring.set_password(", ".read_text("):
        for line in src.splitlines():
            if marker in line:
                assert line.startswith((" ", "\t")), f"module-level I/O: {line!r}"
