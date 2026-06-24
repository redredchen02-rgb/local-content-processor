"""Sina news scraper — uses the feed API."""

from __future__ import annotations

from ..models import GossipItem
from .base import fetch_json, tag_from_title

_SINA_NEWS = "https://feed.mix.sina.com.cn/api/roll/get"
_EXTRA_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://news.sina.com.cn",
}


class SinaScraper:
    platform = "sina"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        data = await fetch_json(
            _SINA_NEWS,
            headers=_EXTRA_HEADERS,
            params={
                "pageid": "153",
                "lid": "2515",
                "k": "",
                "num": str(min(limit, 50)),
                "page": "1",
            },
        )

        items_list = data.get("result", {}).get("data", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(items_list[:limit]):
            title = entry.get("title", "")
            url = entry.get("url", "")
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


