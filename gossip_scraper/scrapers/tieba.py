"""Tieba (百度贴吧) hot topics scraper — uses the public hottopic API."""

from __future__ import annotations

from ..models import GossipItem
from .base import fetch_json

_TIEBA_HOT = "https://tieba.baidu.com/hottopic/browse/topicList"
_EXTRA_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://tieba.baidu.com",
}


class TiebaScraper:
    platform = "tieba"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        data = await fetch_json(_TIEBA_HOT, headers=_EXTRA_HEADERS)

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
