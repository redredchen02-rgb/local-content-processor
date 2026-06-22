"""Douban movie hot scraper — uses the public search_subjects API."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_DOUBAN_MOVIE = "https://movie.douban.com/j/search_subjects"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://movie.douban.com",
}


class DoubanMovieScraper:
    platform = "douban_movie"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _DOUBAN_MOVIE,
                headers=_HEADERS,
                params={"type": "movie", "tag": "热门", "page_limit": min(limit, 50)},
            )
            resp.raise_for_status()
            data = resp.json()

        items_list = data.get("subjects", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(items_list[:limit]):
            title = entry.get("title", "")
            rate = entry.get("rate", "0")
            url = entry.get("url", "")
            try:
                heat = int(float(rate) * 100000) if rate else 0
            except (ValueError, TypeError):
                heat = 0
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=title,
                    url=url,
                    heat=heat,
                    tag="影视",
                )
            )
        return items
