"""Bilibili popular videos scraper — uses the public popular API."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_BILIBILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}


class BilibiliPopularScraper:
    platform = "bilibili_popular"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _BILIBILI_POPULAR,
                headers=_HEADERS,
                params={"ps": min(limit, 50), "pn": 1},
            )
            resp.raise_for_status()
            data = resp.json()

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
