"""Douban movie hot scraper — uses the public search_subjects API.

Returns trending movies with rank-position-based heat (position 1 = hottest).
Paginates in batches of 20 (Douban API hard limit per request) to reach `limit`."""

from __future__ import annotations

from .base import fetch_json
from ..models import GossipItem

_DOUBAN_MOVIE = "https://movie.douban.com/j/search_subjects"
_EXTRA_HEADERS = {
    "Referer": "https://movie.douban.com",
}


_PAGE_SIZE = 20  # Douban API hard limit per request


class DoubanMovieScraper:
    platform = "douban_movie"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        entries: list[dict] = []
        offset = 0
        while len(entries) < limit:
            batch_size = min(_PAGE_SIZE, limit - len(entries))
            data = await fetch_json(
                _DOUBAN_MOVIE,
                headers=_EXTRA_HEADERS,
                params={
                    "type": "movie",
                    "tag": "热门",
                    "page_limit": str(batch_size),
                    "page_start": str(offset),
                },
            )
            batch = data.get("subjects", [])
            if not batch:
                break
            entries.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size

        n = len(entries)
        items: list[GossipItem] = []
        for i, entry in enumerate(entries[:limit]):
            title = entry.get("title", "")
            url = entry.get("url", "")
            # Rank-position-based heat: position 1 (hottest) gets the highest value.
            heat = (n - i) * 10000
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
