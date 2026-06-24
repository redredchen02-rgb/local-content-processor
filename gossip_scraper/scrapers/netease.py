"""Netease hot news scraper — uses the mobile hot news API."""

from __future__ import annotations

from .base import fetch_json, tag_from_title
from ..models import GossipItem

_NETEASE_HOT = "https://m.163.com/fe/api/hot/news/flow"
_EXTRA_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://m.163.com",
}


class NeteaseScraper:
    platform = "netease"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        data = await fetch_json(
            _NETEASE_HOT,
            headers=_EXTRA_HEADERS,
            params={"api_version": "v1", "size": str(min(limit, 100))},
        )

        items_list = data.get("data", {}).get("list", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(items_list[:limit]):
            title = entry.get("title", "")
            url = entry.get("source", "") or entry.get("url", "")
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=title,
                    url=url,
                    heat=max(1, (len(items_list) - i) * 5000),
                    tag=tag_from_title(title),
                )
            )
        return items


