"""Bilibili popular videos scraper — uses the public popular API."""

from __future__ import annotations

from .base import fetch_json
from ..models import GossipItem

_BILIBILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
_EXTRA_HEADERS = {
    "Referer": "https://www.bilibili.com",
}


class BilibiliPopularScraper:
    platform = "bilibili_popular"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        data = await fetch_json(
            _BILIBILI_POPULAR,
            headers=_EXTRA_HEADERS,
            params={"ps": str(min(limit, 50)), "pn": "1"},
        )

        video_list = data.get("data", {}).get("list", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(video_list[:limit]):
            title = entry.get("title", "")
            bvid = entry.get("bvid", "")
            views = entry.get("stat", {}).get("view", 0)
            tag = entry.get("t_label", "")
            url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=title,
                    url=url,
                    heat=int(views) if views else 0,
                    tag=tag if tag else "热",
                )
            )
        return items
