"""Toutiao (今日头条) hot board scraper — uses the public hot-event API."""

from __future__ import annotations

from urllib.parse import quote_plus

import httpx

from ..models import GossipItem

_TOUTIAO_HOT = "https://www.toutiao.com/hot-event/hot-board/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.toutiao.com",
}


class ToutiaoScraper:
    platform = "toutiao"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _TOUTIAO_HOT,
                headers=_HEADERS,
                params={"origin": "toutiao_pc"},
            )
            resp.raise_for_status()
            data = resp.json()

        items_list = data.get("data", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(items_list[:limit]):
            title = entry.get("Title", "")
            hot_value = entry.get("HotValue", 0)
            url = entry.get("Url", "")
            label = entry.get("LabelDesc", "")
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=title,
                    url=url if url else f"https://so.toutiao.com/search?keyword={quote_plus(title)}",
                    heat=int(hot_value) if hot_value else 0,
                    tag=_tag_from_label(label),
                )
            )
        return items


def _tag_from_label(label: str) -> str:
    """Map Toutiao label descriptions to short tags."""
    if not label:
        return ""
    if "热" in label:
        return "热"
    if "新" in label:
        return "新"
    if "爆" in label:
        return "爆"
    if "荐" in label:
        return "荐"
    return label[:2]
