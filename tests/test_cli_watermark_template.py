"""Unit 5: CLI process-time inputs (--watermark / --template / --ai-copy)."""

from __future__ import annotations

import yaml

from lcp.cli import main
from lcp.core.errors import EXIT_OK


def _crawled_job(base, job_id="jw"):
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    ts = "2026-06-17T00:00:00Z"
    store = JobStore(base_dir=base)
    store.create_job(job_id, created_at=ts)
    store.set_state(job_id, JobState.CRAWLED, updated_at=ts)
    raw = store.job_dir(job_id) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "source.txt").write_text("某展览本周末在市中心开幕。", encoding="utf-8")
    return store


def _config(tmp_path, base):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "storage": {"base_dir": base},
                "templates": {"网红黑料": "为 {category} 栏目写作"},
            }
        ),
        encoding="utf-8",
    )
    return str(cfg)


def test_process_accepts_template_watermark_aicopy_flags_dry_run(tmp_path):
    base = str(tmp_path)
    cfg = _config(tmp_path, base)
    _crawled_job(base, "jw")
    rc = main(
        [
            "--config",
            cfg,
            "--dry-run",
            "process",
            "--job-id",
            "jw",
            "--template",
            "网红黑料",
            "--watermark",
            "--ai-copy",
        ]
    )
    assert rc == EXIT_OK


def test_no_watermark_flag_accepted(tmp_path):
    base = str(tmp_path)
    cfg = _config(tmp_path, base)
    _crawled_job(base, "jw2")
    rc = main(
        [
            "--config",
            cfg,
            "--dry-run",
            "process",
            "--job-id",
            "jw2",
            "--no-watermark",
        ]
    )
    assert rc == EXIT_OK


def test_unknown_template_is_tolerated(tmp_path):
    # an unknown category simply means "no template" — process still runs.
    base = str(tmp_path)
    cfg = _config(tmp_path, base)
    _crawled_job(base, "jw3")
    rc = main(
        [
            "--config",
            cfg,
            "--dry-run",
            "process",
            "--job-id",
            "jw3",
            "--template",
            "不存在的栏目",
        ]
    )
    assert rc == EXIT_OK
