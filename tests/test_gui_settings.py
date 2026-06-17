"""LLM settings via the GUI Api + config helpers.

CORE INVARIANT under test: the api_key NEVER lands in a file — base_url/model
(and the host) go to the YAML config; the key goes ONLY to the OS keyring. All
keyring access is monkeypatched so these tests never touch the real keychain.
"""

import os

import keyring
import pytest
import yaml

import lcp.core.config as config
from lcp.core.errors import InputValidationError
from lcp.gui import Api

SECRET = "sk-test-DO-NOT-PERSIST-0123456789abcdef"
BASE = "https://la-sealion.example.com/v1"
HOST = "la-sealion.example.com"
MODEL = "gemma4-31b-heretic"


def _api(tmp_path):
    return Api(config_path=str(tmp_path / "config.yaml"))


# --- save_settings: file gets base_url/model/host, key goes to keyring --------


def test_save_settings_writes_base_url_model_host_never_key(tmp_path, monkeypatch):
    stored = {}
    monkeypatch.setattr(
        config, "set_llm_api_key",
        lambda secret, **kw: stored.update(secret=secret, kw=kw),
    )
    api = _api(tmp_path)
    res = api.save_settings(BASE, MODEL, SECRET)

    assert "error" not in res, res
    assert res["saved"] is True
    assert res["key_saved"] is True
    # The keyring setter received the REAL secret...
    assert stored["secret"] == SECRET
    assert stored["kw"]["username"] == "llm"

    # ...but the file holds base_url/model/host and absolutely NO key.
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert SECRET not in text
    data = yaml.safe_load(text)
    assert data["llm"]["base_url"] == BASE
    assert data["llm"]["model"] == MODEL
    assert HOST in data["llm"]["allowed_hosts"]
    assert "api_key" not in data["llm"]


def test_save_settings_empty_key_does_not_call_keyring(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(config, "set_llm_api_key", lambda *a, **k: called.append(1))
    api = _api(tmp_path)
    res = api.save_settings(BASE, MODEL, "")
    assert "error" not in res
    assert res["key_saved"] is False
    assert called == []


def test_save_settings_merges_and_preserves_other_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "set_llm_api_key", lambda *a, **k: None)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "storage": {"base_dir": "./data"},
                "publisher": {"reviewers": ["alice"]},
                "llm": {"keyring_username": "llm", "allowed_hosts": ["keep.example.com"]},
            }
        ),
        encoding="utf-8",
    )
    api = Api(config_path=str(cfg))
    res = api.save_settings(BASE, MODEL, "")
    assert "error" not in res

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["storage"]["base_dir"] == "./data"
    assert data["publisher"]["reviewers"] == ["alice"]
    # both the pre-existing host and the new one are kept (no dupes)
    assert "keep.example.com" in data["llm"]["allowed_hosts"]
    assert HOST in data["llm"]["allowed_hosts"]


def test_save_settings_rejects_bad_base_url_and_writes_nothing(tmp_path):
    api = _api(tmp_path)
    res = api.save_settings("ftp://x/v1", MODEL, "")
    assert "error" in res and res["exit_code"] == 2
    res2 = api.save_settings("https://x.example.com/openai", MODEL, "")  # no /v1
    assert "error" in res2 and res2["exit_code"] == 2
    assert not (tmp_path / "config.yaml").exists()


def test_save_settings_keyring_failure_persists_nothing(tmp_path, monkeypatch):
    """P2 regression: the keyring write happens FIRST. If it fails, the config
    file is NOT written (no partial state) and no secret reaches the file."""
    from lcp.core.errors import DependencyError

    def _boom(*a, **k):
        raise DependencyError("no keyring backend")

    monkeypatch.setattr(config, "set_llm_api_key", _boom)
    api = _api(tmp_path)
    res = api.save_settings(BASE, MODEL, SECRET)
    assert "error" in res and res["exit_code"] == 3
    # Nothing persisted, and certainly not the secret.
    assert not (tmp_path / "config.yaml").exists()


def test_save_settings_http_loopback_round_trips_to_client(tmp_path, monkeypatch):
    """P2 regression: a loopback http endpoint saved via the GUI must be ACCEPTED
    by the client transport gate — so the host is added to allow_http_hosts too."""
    monkeypatch.setattr(config, "set_llm_api_key", lambda *a, **k: None)
    api = _api(tmp_path)
    res = api.save_settings("http://127.0.0.1:11434/v1", "local-model", "")
    assert "error" not in res, res

    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "127.0.0.1" in data["llm"]["allowed_hosts"]
    assert "127.0.0.1" in data["llm"]["allow_http_hosts"]

    # The client's own transport validator now accepts it (would raise before).
    from lcp.adapters.llm.client import _validate_base_url

    scheme, host = _validate_base_url(
        "http://127.0.0.1:11434/v1",
        data["llm"]["allowed_hosts"],
        frozenset(data["llm"]["allow_http_hosts"]),
    )
    assert (scheme, host) == ("http", "127.0.0.1")


# --- get_settings: reports key presence as a bool, never the secret ----------


def test_get_settings_key_set_via_env_without_revealing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "set_llm_api_key", lambda *a, **k: None)
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    api = _api(tmp_path)
    api.save_settings(BASE, MODEL, "")

    monkeypatch.delenv("LCP_LLM_API_KEY", raising=False)
    res = api.get_settings()
    assert res["api_key_set"] is False
    assert res["base_url"] == BASE
    assert res["model"] == MODEL

    monkeypatch.setenv("LCP_LLM_API_KEY", SECRET)
    res2 = api.get_settings()
    assert res2["api_key_set"] is True
    assert SECRET not in str(res2)  # the bool, never the secret


# --- get_settings: allow_domains is exposed READ-ONLY (Phase 0, onboarding P3) -


def _write_crawler_config(tmp_path, allow_domains):
    """Write a minimal config.yaml carrying a crawler.allow_domains list."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"crawler": {"allow_domains": allow_domains}}),
        encoding="utf-8",
    )
    return cfg


def test_get_settings_exposes_allow_domains(tmp_path, monkeypatch):
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    _write_crawler_config(tmp_path, ["a.example", "b.example"])
    res = _api(tmp_path).get_settings()
    assert "error" not in res, res
    assert res["allow_domains"] == ["a.example", "b.example"]


def test_get_settings_empty_allow_domains_is_empty_list(tmp_path, monkeypatch):
    """Default/empty allowlist surfaces as [] so onboarding reads P3 as MISSING."""
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    _write_crawler_config(tmp_path, [])
    res = _api(tmp_path).get_settings()
    assert res["allow_domains"] == []


def test_get_settings_allow_domains_are_html_escaped(tmp_path, monkeypatch):
    """Mirror the allowed_hosts escaping: never pass a raw HTML-special value to
    the bridge (defence in depth — the GUI also renders via textContent)."""
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    _write_crawler_config(tmp_path, ["a&b<x"])
    res = _api(tmp_path).get_settings()
    assert res["allow_domains"] == ["a&amp;b&lt;x"]
    assert "a&b<x" not in res["allow_domains"]


def test_get_settings_returns_a_known_fixed_key_set(tmp_path, monkeypatch):
    """Scope guard (review): Phase 0 adds EXACTLY one key (allow_domains) — no
    other config (reviewers/storage/...) leaks into the settings bridge dict."""
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    _write_crawler_config(tmp_path, ["a.example"])
    res = _api(tmp_path).get_settings()
    assert set(res.keys()) == {
        "base_url",
        "model",
        "allowed_hosts",
        "allow_domains",
        "api_key_set",
        "config_path",
    }


# --- config-level helpers ----------------------------------------------------


def test_update_llm_config_file_is_0600_and_keyless(tmp_path):
    p = tmp_path / "config.yaml"
    config.update_llm_config_file(p, base_url=BASE, model=MODEL, allowed_hosts_add=HOST)
    assert os.stat(p).st_mode & 0o077 == 0  # owner-only (0600)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "api_key" not in data.get("llm", {})


def test_update_llm_config_file_strips_stray_api_key(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump({"llm": {"api_key": "sk-leak", "model": "old"}}),
        encoding="utf-8",
    )
    config.update_llm_config_file(p, base_url=BASE)
    text = p.read_text(encoding="utf-8")
    assert "sk-leak" not in text
    data = yaml.safe_load(text)
    assert "api_key" not in data["llm"]
    assert data["llm"]["base_url"] == BASE


def test_validate_llm_base_url():
    assert config.validate_llm_base_url(BASE) == HOST
    assert config.validate_llm_base_url("http://localhost:1234/v1") == "localhost"
    assert config.validate_llm_base_url("http://127.0.0.1:8000/v1") == "127.0.0.1"
    with pytest.raises(InputValidationError):
        config.validate_llm_base_url("https://x.example.com/openai")  # no /v1
    with pytest.raises(InputValidationError):
        config.validate_llm_base_url("ftp://x.example.com/v1")  # bad scheme
    with pytest.raises(InputValidationError):
        config.validate_llm_base_url("http://public.example.com/v1")  # http non-loopback
    with pytest.raises(InputValidationError):
        config.validate_llm_base_url("")


def test_validate_llm_base_url_refuses_http_to_metadata_and_private():
    """http to link-local (cloud metadata SSRF) and private hosts is REFUSED;
    only loopback http is allowed. https to any host is fine."""
    with pytest.raises(InputValidationError):
        config.validate_llm_base_url("http://169.254.169.254/v1")  # metadata SSRF
    with pytest.raises(InputValidationError):
        config.validate_llm_base_url("http://10.0.0.5/v1")  # private over http
    # https to the same hosts is shape-valid (transport gate is the client).
    assert config.validate_llm_base_url("https://169.254.169.254/v1") == "169.254.169.254"


def test_set_llm_api_key_empty_raises():
    with pytest.raises(InputValidationError):
        config.set_llm_api_key("")
    with pytest.raises(InputValidationError):
        config.set_llm_api_key("   ")


def test_has_api_key_reflects_env(monkeypatch):
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    cfg = config.Config()
    monkeypatch.delenv("LCP_LLM_API_KEY", raising=False)
    assert cfg.has_api_key() is False
    monkeypatch.setenv("LCP_LLM_API_KEY", SECRET)
    assert cfg.has_api_key() is True


# --- static UI guard: the new settings panel stays XSS-safe ------------------


def test_index_html_settings_panel_has_no_inline_handlers():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="settings"' in html
    assert 'id="settings-api-key"' in html
    assert 'type="password"' in html  # the key field is masked
    assert "onclick" not in html
