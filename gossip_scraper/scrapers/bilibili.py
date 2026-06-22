"""Bilibili trending scraper — uses the public search square API."""

from __future__ import annotations

from urllib.parse import quote_plus

import httpx

from ..models import GossipItem

_BILIBILI_TRENDING = "https://api.bilibili.com/x/web-interface/wbi/search/square"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}


class BilibiliScraper:
    platform = "bilibili"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _BILIBILI_TRENDING,
                headers=_HEADERS,
                params={"limit": min(limit, 40)},
            )
            resp.raise_for_status()
            data = resp.json()

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
