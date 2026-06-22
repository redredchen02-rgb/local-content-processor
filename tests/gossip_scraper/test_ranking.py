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


def test_velocity_bonus_raises_surprise_score() -> None:
    # A rising item (trend_velocity > 0) must have a higher surprise_score than
    # an identical item with no velocity — velocity_bonus is absorbed into surprise_score.
    base = _item("weibo", 1, "普通消息", heat=5000)
    rising = _item("weibo", 1, "普通消息", heat=5000)
    rising.trend_velocity = 2.0  # max bonus = min(0.1, 2.0 * 0.05) = 0.1

    out_base = rank([base])
    out_rising = rank([rising])
    assert out_rising[0].surprise_score > out_base[0].surprise_score
    assert out_rising[0].surprise_score <= 1.0


def test_sentiment_contributes_to_surprise_score() -> None:
    # Angry/negative sentiment must score higher than neutral.
    neutral = _item("weibo", 1, "普通播报", heat=5000)
    neutral.sentiment = "neutral"
    angry = _item("weibo", 1, "普通播报", heat=5000)
    angry.sentiment = "anger"

    out_neutral = rank([neutral])
    out_angry = rank([angry])
    assert out_angry[0].surprise_score > out_neutral[0].surprise_score


def test_sort_by_surprise_consistent_with_score() -> None:
    # velocity_bonus is now absorbed into surprise_score, so the item with the
    # higher surprise_score must also rank higher under sort_by='surprise'.
    low_vel = _item("weibo", 1, "平淡消息", heat=5000)
    low_vel.trend_velocity = 0.0
    high_vel = _item("weibo", 2, "平淡消息", heat=5000)
    high_vel.trend_velocity = 2.0

    by_surprise = rank([low_vel, high_vel], sort_by="surprise")
    assert by_surprise[0].trend_velocity == 2.0


def test_rank_empty() -> None:
    assert rank([]) == []
