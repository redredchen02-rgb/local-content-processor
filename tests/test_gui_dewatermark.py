"""Unit 9: de-watermark GUI Api + CLI parity + render discipline."""

from __future__ import annotations

from pathlib import Path

import yaml

from lcp.cli import main
from lcp.core.errors import EXIT_OK
from lcp.gui import Api

APP_JS = Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "app.js"
LEX_JS = Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "lex.js"


def _cfg(tmp_path, base, reviewers=("alice", "bob")):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "storage": {"base_dir": base},
        "publisher": {"reviewers": list(reviewers)},
    }), encoding="utf-8")
    return str(cfg)


def _job(base, job_id="j1"):
    from lcp.adapters.storage.job_store import JobStore
    store = JobStore(base_dir=base)
    store.create_job(job_id, created_at="2026-06-17T00:00:00Z")
    return store


def test_status_default_locked(tmp_path):
    base = str(tmp_path)
    api = Api(config_path=_cfg(tmp_path, base))
    _job(base, "j1")
    st = api.dewatermark_status("j1")
    assert st["attested"] is False
    assert st["engine_ready"] is False  # no engine configured by default


def test_request_then_attest_unlocks(tmp_path):
    base = str(tmp_path)
    api = Api(config_path=_cfg(tmp_path, base))
    _job(base, "j1")
    assert "error" not in api.request_dewatermark("j1", "bob")
    res = api.attest_dewatermark("j1", "alice", "contract-7")
    assert res["attested"] is True
    assert "ATTESTATION, NOT AUTHENTICATION" in res["disclaimer"]
    assert api.dewatermark_status("j1")["attested"] is True


def test_reviewer_equals_submitter_blocked_via_api(tmp_path):
    base = str(tmp_path)
    api = Api(config_path=_cfg(tmp_path, base))
    _job(base, "j1")
    api.request_dewatermark("j1", "alice")
    res = api.attest_dewatermark("j1", "alice", "contract-7")
    assert "error" in res  # SoD violation
    assert api.dewatermark_status("j1")["attested"] is False


def test_disclaimer_exposed(tmp_path):
    base = str(tmp_path)
    api = Api(config_path=_cfg(tmp_path, base))
    assert "ATTESTATION, NOT AUTHENTICATION" in api.dewatermark_disclaimer()["disclaimer"]


def test_cli_parity_request_and_attest(tmp_path):
    base = str(tmp_path)
    cfg = _cfg(tmp_path, base)
    _job(base, "jc")
    assert main(["--config", cfg, "dewatermark-request", "--job-id", "jc", "--submitter", "bob"]) == EXIT_OK
    assert main([
        "--config", cfg, "dewatermark-attest", "--job-id", "jc",
        "--reviewer", "alice", "--evidence", "contract-9",
    ]) == EXIT_OK
    # GUI sees the CLI-made attestation (parity)
    api = Api(config_path=cfg)
    assert api.dewatermark_status("jc")["attested"] is True


def test_cli_reviewer_equals_submitter_nonzero(tmp_path):
    base = str(tmp_path)
    cfg = _cfg(tmp_path, base)
    _job(base, "jc")
    main(["--config", cfg, "dewatermark-request", "--job-id", "jc", "--submitter", "alice"])
    rc = main([
        "--config", cfg, "dewatermark-attest", "--job-id", "jc",
        "--reviewer", "alice", "--evidence", "c-1",
    ])
    assert rc != EXIT_OK  # segregation-of-duties refusal


# --- render discipline -------------------------------------------------------


def test_app_js_dewatermark_surface_and_poll_cap():
    src = APP_JS.read_text(encoding="utf-8")
    assert "renderDewatermark" in src
    assert "attest_dewatermark" in src and "request_dewatermark" in src
    assert "POLL_CAP_INPAINT" in src and "process_dewatermark" in src
    assert "innerHTML" not in src


def test_lex_has_dewatermark_honesty():
    assert "dewatermark_attest" in LEX_JS.read_text(encoding="utf-8")
