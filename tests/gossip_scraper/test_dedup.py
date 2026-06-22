"""Tests for cross-platform dedup — verifies it is platform-agnostic (no edits
needed for a new platform like Douyin) and backfills coverage for the
previously-untested module."""

from __future__ import annotations

from gossip_scraper.core.dedup import dedup
from gossip_scraper.models import GossipItem


def _item(platform: str, rank: int, title: str, heat: int = 0) -> GossipItem:
    return GossipItem(platform=platform, rank=rank, title=title, heat=heat)


def test_same_event_across_platforms_merges_including_douyin() -> None:
    items = [
        _item("weibo", 1, "吴磊新剧今晚开播", heat=900),
        _item("douyin", 1, "吴磊新剧开播了", heat=500),
        _item("baidu", 1, "吴磊新剧首播", heat=700),
    ]
    out = dedup(items)
    assert len(out) == 1
    m = out[0]
    assert m.cross_platform_count == 3
    assert m.merged_from == sorted(["weibo", "douyin", "baidu"])
    assert m.heat == 900  # keeps the highest-heat entry
    assert m.platform == "weibo"


def test_unrelated_events_do_not_merge() -> None:
    items = [
        _item("weibo", 1, "吴磊新剧今晚开播", heat=900),
        _item("douyin", 2, "某地突发暴雨红色预警", heat=800),
        _item("baidu", 3, "国足世预赛大名单公布", heat=700),
    ]
    out = dedup(items)
    assert len(out) == 3  # nothing merges


def test_single_item_passthrough() -> None:
    out = dedup([_item("douyin", 1, "独家爆料", heat=100)])
    assert len(out) == 1
    assert out[0].cross_platform_count == 1
    assert out[0].merged_from == ["douyin"]


def test_empty_returns_empty() -> None:
    assert dedup([]) == []


def test_short_shared_prefix_overmerges_known_limitation() -> None:
    # KNOWN LIMITATION (dedup-quality residual in the plan): distinct events that
    # share a short leading name (e.g. a celebrity) over-merge at the 0.30
    # threshold — LCS('吴磊') ratio 0.5 >= 0.30. Characterized here (CURRENT
    # behavior, not desired) so a future dedup-quality pass has an anchor.
    items = [
        _item("weibo", 1, "吴磊新剧", heat=500),
        _item("douyin", 2, "吴磊塌房", heat=400),
    ]
    out = dedup(items)
    assert len(out) == 1  # over-merges
