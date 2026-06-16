import pytest

from lcp.core.config import Config, load_config
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
    import lcp.core.config as cfgmod

    monkeypatch.setattr(
        "keyring.get_password", lambda *a, **k: None, raising=False
    )
    cfg = Config()
    with pytest.raises(DependencyError) as ei:
        cfg.llm_api_key()
    # Error message must not leak any secret.
    assert "REDACTED" not in str(ei.value)


def test_api_key_from_env_fallback(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: None, raising=False)
    monkeypatch.setenv("LCP_LLM_API_KEY", "env-secret-123")
    assert Config().llm_api_key() == "env-secret-123"
