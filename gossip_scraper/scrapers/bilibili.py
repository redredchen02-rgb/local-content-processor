"""Bilibili trending scraper — uses the public search square API."""

from __future__ import annotations

from urllib.parse import quote_plus

from ..models import GossipItem
from .base import fetch_json

_BILIBILI_TRENDING = "https://api.bilibili.com/x/web-interface/wbi/search/square"
_EXTRA_HEADERS = {
    "Referer": "https://www.bilibili.com",
}


class BilibiliScraper:
    platform = "bilibili"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        data = await fetch_json(
            _BILIBILI_TRENDING,
            headers=_EXTRA_HEADERS,
            params={"limit": str(min(limit, 40))},
        )

        trending = data.get("data", {}).get("trending", {}).get("list", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(trending[:limit]):
            keyword = entry.get("keyword", "")
            heat = entry.get("heat_score", 0)
            icon = entry.get("icon", "")
            tag = _tag_from_icon(icon)
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=keyword,
                    url=f"https://search.bilibili.com/all?keyword={quote_plus(keyword)}",
                    heat=int(heat) if heat else 0,
                    tag=tag,
                )
            )
        return items


def _tag_from_icon(icon: str) -> str:
    """Extract tag from bilibili icon URL (e.g. 'new', 'hot', 'recom')."""
    if not icon:
        return ""
    lower = icon.lower()
    if "new" in lower:
        return "新"
    if "hot" in lower:
        return "热"
    if "recom" in lower:
        return "荐"
    return ""
