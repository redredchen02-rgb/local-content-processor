"""Unit 1: the CLI auto-discovers config.yaml from the current working directory.

Regression coverage for the bug where `lcp run` (no --config) ignored the
config.yaml that `lcp init` writes — `Ctx` did `load_config(None)` -> defaults,
never reading the file. These drive the REAL no---config + cwd-config.yaml path the
existing suite never exercised (it always passes explicit --config/--output-dir).

All tests `chdir` to a clean tmp so they never depend on a developer's repo-root
config.yaml (and prove cwd-relative resolution).
"""

import pytest
import yaml

from lcp.cli import Ctx, main
from lcp.core.errors import InputValidationError


def _write_config(path, *, allow_domains=("example.com",), base_dir):
    path.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": base_dir}, "crawler": {"allow_domains": list(allow_domains)}}
        ),
        encoding="utf-8",
    )


def test_ctx_auto_loads_cwd_config_when_no_config_flag(tmp_path, monkeypatch):
    # The regression: a cwd config.yaml must be read when no --config is given.
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path / "config.yaml", allow_domains=["example.com"], base_dir=str(tmp_path / "data"))
    ctx = Ctx({})  # no config_path -> must pick up ./config.yaml, not defaults
    assert ctx.config.crawler.allow_domains == ["example.com"]


def test_ctx_no_config_no_file_uses_defaults(tmp_path, monkeypatch):
    # Fresh dir, no config.yaml, no --config -> defaults, NO raise (CI-safe).
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "config.yaml").exists()
    ctx = Ctx({})
    assert ctx.config.crawler.allow_domains == []  # empty = open-crawl default


def test_explicit_config_path_is_honoured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    custom = tmp_path / "custom.yaml"
    _write_config(custom, allow_domains=["other.example"], base_dir=str(tmp_path / "data"))
    ctx = Ctx({"config_path": str(custom)})
    assert ctx.config.crawler.allow_domains == ["other.example"]


def test_explicit_beats_cwd_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path / "config.yaml", allow_domains=["cwd.example"], base_dir=str(tmp_path / "data"))
    custom = tmp_path / "custom.yaml"
    _write_config(custom, allow_domains=["explicit.example"], base_dir=str(tmp_path / "data"))
    ctx = Ctx({"config_path": str(custom)})
    assert ctx.config.crawler.allow_domains == ["explicit.example"]  # explicit wins over cwd


def test_dir_named_config_yaml_falls_through_to_defaults(tmp_path, monkeypatch):
    # A *directory* named config.yaml must not be handed to load_config (which
    # would read_text it -> IsADirectoryError -> exit 5). is_file() ignores it, so
    # Ctx falls through to defaults cleanly.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").mkdir()
    ctx = Ctx({})
    assert ctx.config.crawler.allow_domains == []  # defaults, no crash


def test_explicit_missing_config_still_raises(tmp_path, monkeypatch):
    # The new implicit default must NOT mask a typo'd explicit path into defaults.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(InputValidationError):
        Ctx({"config_path": str(tmp_path / "does-not-exist.yaml")})


def test_init_then_command_honours_config(tmp_path, monkeypatch):
    # Stronger integration: the real `lcp init` -> edit -> command flow. init writes
    # config.yaml in cwd; a subsequent Ctx (no --config) must read it.
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    cfg = tmp_path / "config.yaml"
    assert cfg.exists()
    # Edit it the way the init message tells the operator to (add an allow_domains entry).
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data.setdefault("crawler", {})["allow_domains"] = ["example.com"]
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    # A command-level Ctx with no --config now sees the edited config.
    ctx = Ctx({})
    assert ctx.config.crawler.allow_domains == ["example.com"]
