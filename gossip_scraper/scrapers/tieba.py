"""Tieba (百度贴吧) hot topics scraper — uses the public hottopic API."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_TIEBA_HOT = "https://tieba.baidu.com/hottopic/browse/topicList"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://tieba.baidu.com",
}


class TiebaScraper:
    platform = "tieba"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_TIEBA_HOT, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()

        topic_list = data.get("data", {}).get("bang_topic", {}).get("topic_list", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(topic_list[:limit]):
            name = entry.get("topic_name", "")
            heat = entry.get("discuss_num", 0)
            url = entry.get("topic_url", "")
            create_time = entry.get("create_time", 0)
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=name,
                    url=url,
                    heat=int(heat) if heat else 0,
                    created_at=float(create_time) if create_time else 0.0,
                    tag="热",
                )
            )
        return items
