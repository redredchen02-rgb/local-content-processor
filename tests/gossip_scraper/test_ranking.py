"""Tests for 3-dimension ranking — verifies a new platform (Douyin) is scored
(no platform exclusion) and degrades gracefully without the baidu/tieba-specific
velocity/recency bonuses. Backfills coverage for the previously-untested module."""

from __future__ import annotations

from gossip_scraper.core.ranking import rank
from gossip_scraper.models import GossipItem


def _item(platform: str, rank_: int, title: str, heat: int = 0) -> GossipItem:
    return GossipItem(platform=platform, rank=rank_, title=title, heat=heat)


def test_higher_heat_ranks_first_same_platform() -> None:
    items = [
        _item("weibo", 1, "低热度普通消息", heat=100),
        _item("weibo", 2, "高热度大瓜实锤", heat=10000),
    ]
    out = rank(items)
    assert out[0].title == "高热度大瓜实锤"
    assert [it.rank for it in out] == [1, 2]
    assert out[0].score >= out[1].score


def test_douyin_item_is_scored_no_exclusion() -> None:
    out = rank([_item("douyin", 1, "某明星突发大瓜实锤", heat=5000)])
    assert len(out) == 1
    assert out[0].score > 0  # douyin participates in scoring
    assert out[0].surprise_score > 0  # surprise keywords detected on douyin items


def test_douyin_degrades_without_velocity_or_recency() -> None:
    # douyin has no hot_change (baidu) or created_at (tieba); it must still score
    # on heat + rank + surprise without error.
    items = [
        _item("douyin", 1, "抖音大瓜", heat=8000),
        _item("baidu", 2, "百度热搜", heat=8000),
    ]
    out = rank(items)
    assert all(it.score >= 0 for it in out)
    assert {it.platform for it in out} == {"douyin", "baidu"}


def test_rank_renumbers_and_sorts_descending() -> None:
    items = [_item("weibo", i, f"消息{i}", heat=i * 100) for i in range(1, 6)]
    out = rank(items)
    assert len(out) == 5
    assert [it.rank for it in out] == [1, 2, 3, 4, 5]
    assert all(out[i].score >= out[i + 1].score for i in range(len(out) - 1))


def test_sort_by_surprise_dimension() -> None:
    items = [
        _item("weibo", 1, "平淡的日常播报", heat=5000),
        _item("weibo", 2, "震惊全网塌房实锤曝光", heat=5000),
    ]
    out = rank(items, sort_by="surprise")
    assert out[0].title == "震惊全网塌房实锤曝光"


def test_rank_empty() -> None:
    assert rank([]) == []
