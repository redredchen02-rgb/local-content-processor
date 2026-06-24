"""Tests for trend velocity tracking — covers the 4 previously-unfixed defects:
cwd-independent history path, IndexError on malformed history, title-only item key,
and atomic write via tmp+rename."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gossip_scraper.core.trend import (
    _DEFAULT_HISTORY_DIR,
    _item_key,
    compute_velocity,
    save_velocity_history,
)
from gossip_scraper.models import GossipItem


def _item(title: str, platform: str = "weibo", rank: int = 1) -> GossipItem:
    return GossipItem(platform=platform, rank=rank, title=title, heat=1000)


# ---------------------------------------------------------------------------
# _DEFAULT_HISTORY_DIR is cwd-independent
# ---------------------------------------------------------------------------


def test_default_history_dir_is_absolute() -> None:
    assert _DEFAULT_HISTORY_DIR.is_absolute()


def test_default_history_dir_does_not_change_with_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # Import-time constant must stay the same regardless of cwd change
    from gossip_scraper.core.trend import _DEFAULT_HISTORY_DIR as d

    assert d.is_absolute()
    assert d == _DEFAULT_HISTORY_DIR


# ---------------------------------------------------------------------------
# _item_key: title-only (no platform prefix)
# ---------------------------------------------------------------------------


def test_item_key_ignores_platform() -> None:
    a = _item("某明星大瓜爆出了", platform="weibo")
    b = _item("某明星大瓜爆出了", platform="baidu")
    assert _item_key(a) == _item_key(b)


def test_item_key_truncates_at_30() -> None:
    long_title = "A" * 50
    key = _item_key(_item(long_title))
    assert len(key) == 30


# ---------------------------------------------------------------------------
# _load_history: IndexError on malformed values
# ---------------------------------------------------------------------------


def test_load_history_tolerates_malformed_values(tmp_path: Path) -> None:
    # Write a history file where one value is a single-element list instead of [rank, ts]
    bad = {"某话题": [1], "好话题": [2, time.time()]}
    (tmp_path / ".gossip_history.json").write_text(json.dumps(bad), encoding="utf-8")

    # Must not raise — returns empty or only valid entries
    items = [_item("某话题"), _item("好话题")]
    result = compute_velocity(items, history_dir=tmp_path)
    assert result  # ran without crash


def test_load_history_tolerates_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / ".gossip_history.json").write_text("{not valid json", encoding="utf-8")
    items = [_item("话题A")]
    result = compute_velocity(items, history_dir=tmp_path)
    assert result  # ran without crash
    assert result[0].trend_velocity == 0.5  # treated as new topic


# ---------------------------------------------------------------------------
# Atomic write: tmp file replaced, no leftover on success
# ---------------------------------------------------------------------------


def test_save_history_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    items = [_item("话题A", rank=1), _item("话题B", rank=2)]
    save_velocity_history(items, history_dir=tmp_path)

    history_file = tmp_path / ".gossip_history.json"
    tmp_file = tmp_path / ".gossip_history.json.tmp"
    assert history_file.exists()
    assert not tmp_file.exists()  # tmp must be gone after successful replace()


def test_save_history_readable_json(tmp_path: Path) -> None:
    items = [_item("话题A", rank=1)]
    save_velocity_history(items, history_dir=tmp_path)

    data = json.loads((tmp_path / ".gossip_history.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert len(data) == 1


# ---------------------------------------------------------------------------
# Velocity tracking: title-only key survives platform switch
# ---------------------------------------------------------------------------


def test_velocity_survives_platform_switch(tmp_path: Path) -> None:
    # First run: topic carried by weibo at rank 5; save composite rank after ranking.
    run1 = [_item("某明星出轨大瓜", platform="weibo", rank=5)]
    compute_velocity(run1, history_dir=tmp_path)
    save_velocity_history(run1, history_dir=tmp_path)

    # Second run: same topic, now carried by baidu (platform changed), rank improved to 2
    run2 = [_item("某明星出轨大瓜", platform="baidu", rank=2)]
    compute_velocity(run2, history_dir=tmp_path)

    # Should detect rising (prev_rank=5 > current_rank=2 → positive velocity)
    assert run2[0].trend_velocity > 0


def test_new_topic_gets_default_velocity(tmp_path: Path) -> None:
    items = [_item("全新话题首次出现")]
    compute_velocity(items, history_dir=tmp_path)
    assert items[0].trend_velocity == 0.5
