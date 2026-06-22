"""CLI/GUI mirror + run-by-job-id for gossip ingest (U4).

Asserts the CLAUDE.md invariant (every operator action exists in both shells),
the webserver auto-exposes the action, and `run --job-id` (no --url) resolves
the persisted source URL so an ingested job can be crawled by id."""

from __future__ import annotations

import json

from click.testing import CliRunner

from lcp import webserver
from lcp.adapters.storage.job_store import JobStore
from lcp.cli import cli
from lcp.core.state import JobState
from lcp.gui import Api
from tests.support.pipeline_fakes import FakeCrawler

ITEMS = [
    {"platform": "weibo", "rank": 1, "title": "瓜一", "url": "https://s.weibo.com/weibo?q=a"},
    {"platform": "douyin", "rank": 2, "title": "瓜二", "url": "https://www.douyin.com/search/b"},
    {"platform": "weibo", "rank": 3, "title": "bad", "url": "ftp://nope"},
]


def _write_items(tmp_path, items) -> str:
    f = tmp_path / "items.json"
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(f)


def _setup(tmp_path, name=""):
    """Scaffold a runnable workspace via `init` (writes config + seeds the index)
    so subsequent commands have a loadable --config (load_config raises on a
    missing explicit path)."""
    cfg = tmp_path / f"{name}config.yaml"
    data = tmp_path / f"{name}data"
    r = CliRunner().invoke(cli, ["--config", str(cfg), "--output-dir", str(data), "init"])
    assert r.exit_code == 0, r.output
    return cfg, data


def _invoke(cfg, data, *args):
    return CliRunner().invoke(
        cli, ["--config", str(cfg), "--output-dir", str(data), "--json", *args]
    )


def test_cli_ingest_gossip_creates_jobs_and_reports(tmp_path):
    cfg, data = _setup(tmp_path)
    f = _write_items(tmp_path, ITEMS)
    res = _invoke(cfg, data, "ingest-gossip", "--input", f)
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["created_count"] == 2
    assert out["skipped_count"] == 1
    assert out["skipped"][0]["reason"] == "invalid_or_empty_url"
    store = JobStore(base_dir=data)
    for jid in out["created"]:
        rec = store.get_job(jid)
        assert rec is not None and rec.state is JobState.NEW


def test_cli_ingest_gossip_reads_stdin(tmp_path):
    cfg, data = _setup(tmp_path)
    payload = json.dumps(
        [{"platform": "weibo", "title": "x", "url": "https://s.weibo.com/weibo?q=x"}],
        ensure_ascii=False,
    )
    res = CliRunner().invoke(
        cli, ["--config", str(cfg), "--output-dir", str(data), "--json", "ingest-gossip"],
        input=payload,
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["created_count"] == 1


def test_cli_and_gui_ingest_parity(tmp_path):
    # Same input through both shells -> identical (deterministic) job ids.
    items = [{"platform": "weibo", "title": "x", "url": "https://s.weibo.com/weibo?q=parity"}]
    f = _write_items(tmp_path, items)
    cfg, data = _setup(tmp_path, "p")
    cli_res = _invoke(cfg, data, "ingest-gossip", "--input", f)
    cli_created = json.loads(cli_res.output)["created"]

    api = Api(config_path=str(tmp_path / "c2.yaml"), base_dir=str(tmp_path / "d2"))
    gui_out = api.ingest_gossip(json.dumps(items, ensure_ascii=False))

    assert cli_created == gui_out["created"]
    assert gui_out["created_count"] == 1


def test_gui_ingest_escapes_scraped_fields(tmp_path):
    api = Api(config_path=str(tmp_path / "c.yaml"), base_dir=str(tmp_path / "d"))
    # A skipped (bad-scheme) item whose title carries markup must come back escaped.
    out = api.ingest_gossip(
        json.dumps([{"platform": "weibo", "title": "<script>x</script>", "url": "ftp://x"}])
    )
    assert out["skipped_count"] == 1
    assert "<script>" not in out["skipped"][0]["title"]
    assert "&lt;script&gt;" in out["skipped"][0]["title"]


def test_webserver_exposes_ingest_gossip_route():
    # The action is auto-discovered -> POST /api/ingest_gossip exists.
    assert "ingest_gossip" in webserver.public_routes(Api)


def test_run_by_job_id_without_source_errors(tmp_path):
    cfg, data = _setup(tmp_path)
    # A plain job id with no persisted source URL and no --url -> clear UsageError.
    res = CliRunner().invoke(
        cli,
        ["--config", str(cfg), "--output-dir", str(data), "run", "--job-id", "no-such", "--until", "draft"],
    )
    assert res.exit_code != 0
    assert "requires --url" in (res.output + str(res.exception or ""))


def test_run_by_job_id_resolves_persisted_source(tmp_path, monkeypatch):
    # ingest -> run --job-id (no --url) must resolve source.json and crawl.
    import lcp.cli as climod

    monkeypatch.setattr(climod, "build_crawler", lambda *a, **k: FakeCrawler())
    cfg, data = _setup(tmp_path)  # init also seeds an empty index -> UNIQUE at dedup

    f = _write_items(tmp_path, [{"platform": "weibo", "title": "x", "url": "https://s.weibo.com/weibo?q=run"}])
    ing = _invoke(cfg, data, "ingest-gossip", "--input", f)
    jid = json.loads(ing.output)["created"][0]

    res = CliRunner().invoke(
        cli,
        ["--config", str(cfg), "--output-dir", str(data), "--dry-run", "--json",
         "run", "--job-id", jid, "--until", "draft"],
    )
    assert res.exit_code == 0, res.output
    # The job advanced past NEW -> the persisted URL was resolved and stage1 crawled it.
    assert json.loads(res.output)["state"] != JobState.NEW.value
