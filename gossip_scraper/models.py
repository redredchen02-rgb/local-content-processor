"""Base scraper interface for gossip/drama aggregation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class GossipItem:
    """One trending gossip/drama item from any platform."""

    platform: str
    rank: int
    title: str
    url: str = ""
    heat: int = 0
    tag: str = ""
    fetched_at: float = field(default_factory=time.time)
    hot_change: float = 0.0
    created_at: float = 0.0
    cross_platform_count: int = 1
    score: float = 0.0
    heat_score: float = 0.0
    freshness_score: float = 0.0
    surprise_score: float = 0.0
    merged_from: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "rank": self.rank,
            "title": self.title,
            "url": self.url,
            "heat": self.heat,
            "tag": self.tag,
            "hot_change": self.hot_change,
            "created_at": self.created_at,
            "fetched_at": self.fetched_at,
            "cross_platform_count": self.cross_platform_count,
            "score": round(self.score, 3),
            "heat_score": round(self.heat_score, 3),
            "freshness_score": round(self.freshness_score, 3),
            "surprise_score": round(self.surprise_score, 3),
            "merged_from": self.merged_from or [self.platform],
        }
