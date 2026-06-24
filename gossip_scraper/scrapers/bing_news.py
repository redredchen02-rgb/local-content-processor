"""Bing News Chinese RSS scraper — aggregated Chinese news."""

from __future__ import annotations

from .base import fetch_text, parse_rss_items
from ..models import GossipItem

_BING_NEWS = "https://www.bing.com/news/search"


class BingNewsScraper:
    platform = "bing_news"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(
            _BING_NEWS,
            params={"q": "中国 热门", "format": "rss"},
        )
        items = parse_rss_items(xml, self.platform, limit, base_heat=2000)
        # Skip feed title (substring match)
        return [it for it in items if "Bing" not in it.title and "热门" not in it.title[:5]]
