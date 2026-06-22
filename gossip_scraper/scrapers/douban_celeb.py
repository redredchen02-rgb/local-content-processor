"""Douban celebrity hot scraper — uses the public search_subjects API."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_DOUBAN_CELEB = "https://movie.douban.com/j/search_subjects"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://movie.douban.com",
}


class DoubanCelebScraper:
    platform = "douban_celeb"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _DOUBAN_CELEB,
                headers=_HEADERS,
                params={"type": "celebrity", "tag": "热门", "page_limit": min(limit, 50)},
            )
            resp.raise_for_status()
            data = resp.json()

        items_list = data.get("subjects", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(items_list[:limit]):
            name = entry.get("name", "")
            url = entry.get("url", "")
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=name,
                    url=url,
                    heat=max(1, (len(items_list) - i) * 10000),
                    tag="明星",
                )
            )
        return items
