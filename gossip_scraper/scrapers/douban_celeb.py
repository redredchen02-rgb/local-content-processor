"""Douban celebrity hot scraper — uses the search_subjects movie API with hot tag.

Returns trending movies with rank-position-based heat (position 1 = hottest).
Celebrity gossip titles are derived by appending '相关' to each movie title."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_DOUBAN_SEARCH = "https://movie.douban.com/j/search_subjects"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://movie.douban.com",
}


_PAGE_SIZE = 20  # Douban API hard limit per request


class DoubanCelebScraper:
    platform = "douban_celeb"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        # Douban's celebrity search returns movies; we use it to extract
        # celebrity-related trending topics from the hot movies list.
        # The API caps each response at _PAGE_SIZE; paginate to reach `limit`.
        entries: list[dict] = []
        async with httpx.AsyncClient(timeout=15) as client:
            offset = 0
            while len(entries) < limit:
                batch_size = min(_PAGE_SIZE, limit - len(entries))
                resp = await client.get(
                    _DOUBAN_SEARCH,
                    headers=_HEADERS,
                    params={
                        "type": "movie",
                        "tag": "热门",
                        "page_limit": batch_size,
                        "page_start": offset,
                    },
                )
                resp.raise_for_status()
                batch = resp.json().get("subjects", [])
                if not batch:
                    break
                entries.extend(batch)
                if len(batch) < batch_size:
                    break  # API returned fewer than requested — no more pages
                offset += batch_size

        n = len(entries)
        items: list[GossipItem] = []
        for i, entry in enumerate(entries[:limit]):
            title = entry.get("title", "")
            url = entry.get("url", "")
            # Rank-position-based heat: position 1 (hottest) gets the highest value,
            # avoiding the quality-vs-popularity inversion from using the star rating.
            heat = (n - i) * 10000
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=f"{title} 相关",
                    url=url,
                    heat=heat,
                    tag="影视",
                )
            )
        return items
