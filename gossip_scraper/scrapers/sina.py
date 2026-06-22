"""Sina news scraper — uses the feed API."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_SINA_NEWS = "https://feed.mix.sina.com.cn/api/roll/get"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://news.sina.com.cn",
}


class SinaScraper:
    platform = "sina"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _SINA_NEWS,
                headers=_HEADERS,
                params={
                    "pageid": "153",
                    "lid": "2515",
                    "k": "",
                    "num": str(min(limit, 50)),
                    "page": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()

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
                    tag=_tag_from_title(title),
                )
            )
        return items


def _tag_from_title(title: str) -> str:
    if "突发" in title or "刚刚" in title:
        return "突发"
    if "独家" in title:
        return "独家"
    if "热" in title:
        return "热"
    return ""
