"""3-dimension scoring and ranking for cross-platform gossip.

Score = heat_score × 0.50 + freshness_score × 0.25 + surprise_score × 0.25

Each dimension normalized to [0, 1]. Weights are tunable.
Heat = popularity (how many people are talking).
Freshness = recency (how new/rising the topic is).
Surprise = drama (how unexpected/dramatic)."""

from __future__ import annotations

import re
import time

from ..models import GossipItem

# Weights for the 3 dimensions (must sum to 1.0)
W_HEAT = 0.50
W_FRESH = 0.25
W_SURPRISE = 0.25

# Surprise keywords organized by category (weight tier)
# Tier 1 (strong): direct surprise/drama signals
_SURPRISE_TIER1 = (
    "竟然", "居然", "意外", "反转", "震惊", "突发", "刚刚", "重磅",
    "曝光", "实锤", "锤了", "塌房", "翻车", "炸裂", "离谱", "逆天",
    "不敢信", "难以置信", "活久见", "破防", "绷不住",
)
# Tier 2 (medium): emotional/dramatic reactions
_SURPRISE_TIER2 = (
    "疯了", "炸了", "爆了", "沸了", "哭了", "怒了", "笑死", "绝了",
    "无语", "太狠了", "太猛了", "太牛了", "太假了", "太真了",
    "细思极恐", "越想越不对", "细品", "你品", "你细品",
)
# Tier 3 (light): gossip/drama context markers
_SURPRISE_TIER3 = (
    "瓜", "吃瓜", "塌了", "崩了", "裂了", "碎了", "凉了", "挂了",
    "翻了", "栽了", "坑了", "骗了", "瞒了", "藏了", "爆出了",
    "揭露", "揭穿", "内幕", "真相", "黑幕", "潜规则",
    "人设崩塌", "口碑崩盘", "翻车现场", "大型翻车",
    "连夜", "紧急", "立即", "火速", "刚刚公布", "刚刚确认",
)

# Pattern-based detection (regex)
_SURPRISE_PATTERNS: list[tuple[str, float]] = [
    # Contrast: X竟然是Y / X居然是Y
    (r"(竟然|居然|真的)是", 0.7),
    # Reversal: X反转 / X反转了
    (r"反转", 0.8),
    # Contrast: X vs Y (unexpected pairing)
    (r"(?:vs|VS|对|和|与).{2,10}(?:竟然|居然|意外)", 0.7),
    # Exclamation marks (dramatic punctuation)
    (r"[！!]{2,}", 0.3),
    # Question mark + surprise context
    (r"[？?].{0,5}(?:真的|假的|可能|居然)", 0.4),
    # "从X到Y" escalation pattern
    (r"从.{2,8}到.{2,8}", 0.3),
    # Numbers + dramatic context
    (r"\d+[倍%].*(?:暴涨|暴增|暴跌|翻|腰斩)", 0.5),
    # Emotional escalation
    (r"(?:全网|整个|所有).*(?:怒|炸|沸|沸腾)", 0.5),
]

# Tag → surprise score mapping
_TAG_SURPRISE: dict[str, float] = {
    "爆": 1.0,
    "沸": 0.9,
    "热": 0.6,
    "新": 0.4,
    "荐": 0.3,
}


def rank(items: list[GossipItem], sort_by: str = "score") -> list[GossipItem]:
    """Score and sort items by the chosen dimension.

    sort_by: 'score' (default), 'heat', 'fresh', 'surprise'"""
    if not items:
        return []

    # Phase 1: compute per-platform heat ranges for normalization
    by_platform: dict[str, list[GossipItem]] = {}
    for it in items:
        by_platform.setdefault(it.platform, []).append(it)

    heat_ranges: dict[str, tuple[int, int]] = {}
    for plat, plat_items in by_platform.items():
        heats = [it.heat for it in plat_items]
        heat_ranges[plat] = (min(heats), max(heats))

    # Phase 2: compute velocity ranges for Baidu
    baidu_items = by_platform.get("baidu", [])
    if baidu_items:
        changes = [abs(it.hot_change) for it in baidu_items]
        velocity_range = (min(changes), max(changes))
    else:
        velocity_range = (0, 1)

    # Phase 3: compute time ranges for Tieba
    tieba_items = [it for it in items if it.created_at > 0]
    if tieba_items:
        times = [it.created_at for it in tieba_items]
        time_range = (min(times), max(times))
    else:
        time_range = (0, 1)

    now = time.time()

    # Phase 4: score each item
    for it in items:
        it.heat_score = _score_heat(it, heat_ranges)
        it.freshness_score = _score_freshness(
            it, heat_ranges, velocity_range, time_range, now, len(items)
        )
        it.surprise_score = _score_surprise(
            it, velocity_range, len(items)
        )
        it.score = (
            it.heat_score * W_HEAT
            + it.freshness_score * W_FRESH
            + it.surprise_score * W_SURPRISE
        )

    # Phase 5: sort by chosen dimension
    if sort_by == "heat":
        items.sort(key=lambda x: x.heat_score, reverse=True)
    elif sort_by == "fresh":
        items.sort(key=lambda x: x.freshness_score, reverse=True)
    elif sort_by == "surprise":
        items.sort(key=lambda x: x.surprise_score, reverse=True)
    else:
        items.sort(key=lambda x: x.score, reverse=True)

    # Re-assign rank
    for i, it in enumerate(items):
        it.rank = i + 1

    return items


def _score_heat(item: GossipItem, ranges: dict[str, tuple[int, int]]) -> float:
    """Heat dimension: normalized heat within the platform."""
    rng = ranges.get(item.platform, (0, 1))
    return _normalize(item.heat, rng)


def _score_freshness(
    item: GossipItem,
    heat_ranges: dict[str, tuple[int, int]],
    velocity_range: tuple[float, float],
    time_range: tuple[float, float],
    now: float,
    total_items: int,
) -> float:
    """Freshness dimension: rank position + velocity + recency."""
    # 1. Rank position: higher rank = newer (rank 1 = 1.0)
    rank_fresh = max(0, 1.0 - (item.rank - 1) / max(total_items - 1, 1))

    # 2. Velocity (Baidu hotChange): high change = rising fast
    vel_fresh = 0.0
    if item.platform == "baidu" and velocity_range[1] > 0:
        vel_fresh = _normalize(abs(item.hot_change), velocity_range)

    # 3. Recency (Tieba create_time): newer = fresher
    time_fresh = 0.0
    if item.created_at > 0 and time_range[1] > time_range[0]:
        time_fresh = _normalize(item.created_at, time_range)

    return 0.6 * rank_fresh + 0.25 * vel_fresh + 0.15 * time_fresh


def _score_surprise(
    item: GossipItem,
    velocity_range: tuple[float, float],
    total_items: int,
) -> float:
    """Surprise dimension: tag + cross-platform + velocity + keywords + patterns."""
    # 1. Platform tag (爆/沸/热 = more surprising)
    tag_score = _TAG_SURPRISE.get(item.tag, 0.2)

    # 2. Cross-platform count (more platforms = bigger event)
    cross = item.cross_platform_count
    if cross >= 4:
        cross_score = 1.0
    elif cross == 3:
        cross_score = 0.8
    elif cross == 2:
        cross_score = 0.5
    else:
        cross_score = 0.2

    # 3. Velocity spike (sudden rise = surprising)
    vel_score = 0.0
    if item.platform == "baidu" and velocity_range[1] > 0:
        vel_score = _normalize(abs(item.hot_change), velocity_range)

    # 4. Keyword + pattern detection
    kw_score = _score_keywords(item.title)

    return 0.2 * tag_score + 0.4 * cross_score + 0.1 * vel_score + 0.3 * kw_score


def _score_keywords(title: str) -> float:
    """Multi-layer keyword and pattern scoring for surprise detection.

    Returns a score in [0, 1] based on:
    - Tier 1 keywords (strong surprise signals): 0.5 per hit, max 1.0
    - Tier 2 keywords (emotional reactions): 0.3 per hit, max 0.6
    - Tier 3 keywords (gossip context): 0.2 per hit, max 0.4
    - Regex patterns (structural drama): pattern weight, max 0.8
    - Combined and capped at 1.0"""
    title_lower = title.lower()
    score = 0.0

    # Tier 1: strong surprise signals (highest weight)
    t1_hits = sum(1 for kw in _SURPRISE_TIER1 if kw in title_lower)
    score += min(1.0, t1_hits * 0.5)

    # Tier 2: emotional reactions
    t2_hits = sum(1 for kw in _SURPRISE_TIER2 if kw in title_lower)
    score += min(0.6, t2_hits * 0.3)

    # Tier 3: gossip context markers
    t3_hits = sum(1 for kw in _SURPRISE_TIER3 if kw in title_lower)
    score += min(0.4, t3_hits * 0.2)

    # Pattern detection (structural drama signals)
    pattern_score = 0.0
    for pat, weight in _SURPRISE_PATTERNS:
        if re.search(pat, title):
            pattern_score += weight
    score += min(0.8, pattern_score)

    # Bonus: multiple tiers firing together = compounding surprise
    tiers_fired = (1 if t1_hits else 0) + (1 if t2_hits else 0) + (1 if t3_hits else 0)
    if tiers_fired >= 2:
        score *= 1.2  # 20% bonus for cross-tier signals

    return min(1.0, score)


def _normalize(value: int | float, rng: tuple[int | float, int | float]) -> float:
    """Min-max normalize to [0, 1]."""
    lo, hi = rng
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))
