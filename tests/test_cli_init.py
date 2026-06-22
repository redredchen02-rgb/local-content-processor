"""Unit 4: `lcp init` scaffolds a runnable workspace (fixes blocker B1).

Asserts the config is written 0600 (not the example's world-readable 0644), an
empty site index is seeded (so a clean job is UNIQUE, not parked at dedup), the
command never clobbers an existing config, and the GUI bridge mirrors it 1:1.
"""

from __future__ import annotations

import stat

from click.testing import CliRunner

from lcp.cli import cli
from lcp.gui import Api


def _invoke_init(cfg, data):
    return CliRunner().invoke(cli, ["--config", str(cfg), "--output-dir", str(data), "init"])


def test_init_creates_config_0600_and_empty_index(tmp_path):
    cfg = tmp_path / "config.yaml"
    data = tmp_path / "data"
    res = _invoke_init(cfg, data)
    assert res.exit_code == 0, res.output
    assert cfg.exists()
    index = data / "site_index.jsonl"
    assert index.exists()
    assert index.read_text(encoding="utf-8") == ""  # empty = available/HIGH
    mode = stat.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600, oct(mode)  # NOT the example's 0644


def test_init_does_not_clobber_existing_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("storage:\n  base_dir: ./data\n# OPERATOR EDIT\n", encoding="utf-8")
    res = _invoke_init(cfg, tmp_path / "data")
    assert res.exit_code == 0, res.output
    assert "# OPERATOR EDIT" in cfg.read_text(encoding="utf-8")  # untouched


def test_init_idempotent_leaves_existing_index(tmp_path):
    cfg = tmp_path / "config.yaml"
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "site_index.jsonl").write_text(
        '{"job_id": "x", "title": "t", "body": "b"}\n', encoding="utf-8"
    )
    res = _invoke_init(cfg, data)
    assert res.exit_code == 0, res.output
    # an existing index is not truncated
    assert (data / "site_index.jsonl").read_text(encoding="utf-8").strip()


def test_gui_init_mirrors_cli(tmp_path):
    cfg = tmp_path / "config.yaml"
    data = tmp_path / "data"
    out = Api(config_path=str(cfg), base_dir=str(data)).init_workspace()
    assert out.get("config_created") is True
    assert out.get("index_created") is True
    assert cfg.exists()
    assert (data / "site_index.jsonl").exists()
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
