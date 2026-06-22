"""Tests for per-platform scraper health monitoring (R15)."""

from __future__ import annotations

import logging

from gossip_scraper.core import health

_DAY = 24 * 3600


def test_healthy_platform_no_error(tmp_path, caplog) -> None:
    p = tmp_path / "health.jsonl"
    now = 1_000_000.0
    with caplog.at_level(logging.ERROR, logger="gossip_scraper.health"):
        for i in range(6):
            rate = health.record("weibo", ok=True, item_count=30, path=p, now=now + i * _DAY)
    assert rate == 1.0
    assert not caplog.records  # no ERROR for a healthy platform


def test_sustained_failure_emits_error(tmp_path, caplog) -> None:
    p = tmp_path / "health.jsonl"
    now = 1_000_000.0
    # 5 runs: 1 ok, 4 fail -> 20% < 60%
    health.record("douyin", ok=True, item_count=5, path=p, now=now)
    with caplog.at_level(logging.ERROR, logger="gossip_scraper.health"):
        for i in range(1, 5):
            rate = health.record("douyin", ok=False, item_count=0, path=p, now=now + i * 3600)
    assert rate == 0.2
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errs, "expected an ERROR for a sustained-failing platform"
    msg = errs[-1].getMessage()
    assert "douyin" in msg and "20%" in msg and "success rate" in msg


def test_insufficient_data_no_alarm(tmp_path, caplog) -> None:
    p = tmp_path / "health.jsonl"
    now = 1_000_000.0
    # only 2 records (< _MIN_SAMPLES) — both failures, but not enough to judge
    with caplog.at_level(logging.ERROR, logger="gossip_scraper.health"):
        health.record("baidu", ok=False, item_count=0, path=p, now=now)
        rate = health.record("baidu", ok=False, item_count=0, path=p, now=now + 3600)
    assert rate is None
    assert not caplog.records


def test_zero_runs_in_window_not_reported(tmp_path) -> None:
    p = tmp_path / "health.jsonl"
    now = 1_000_000.0
    # records exist but all OUTSIDE the 7-day window (older than now - window)
    old = now - 30 * _DAY
    for i in range(5):
        health.record("tieba", ok=False, item_count=0, path=p, now=old + i)
    rate = health.record("tieba", ok=True, item_count=10, path=p, now=now)
    # only the single in-window record counts -> below _MIN_SAMPLES -> None
    assert rate is None


def test_per_platform_isolation(tmp_path) -> None:
    p = tmp_path / "health.jsonl"
    now = 1_000_000.0
    for i in range(4):
        health.record("weibo", ok=True, item_count=20, path=p, now=now + i * 3600)
        health.record("douyin", ok=False, item_count=0, path=p, now=now + i * 3600)
    weibo_rate = health.record("weibo", ok=True, item_count=20, path=p, now=now + 5 * 3600)
    douyin_rate = health.record("douyin", ok=False, item_count=0, path=p, now=now + 5 * 3600)
    assert weibo_rate == 1.0
    assert douyin_rate == 0.0
