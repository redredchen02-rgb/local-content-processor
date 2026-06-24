"""Bing News Chinese RSS scraper — aggregated Chinese news."""

from __future__ import annotations

import re

from .base import fetch_text, unescape_html, tag_from_title
from ..models import GossipItem

_BING_NEWS = "https://www.bing.com/news/search"


class BingNewsScraper:
    platform = "bing_news"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(
            _BING_NEWS,
            params={"q": "中国 热门", "format": "rss"},
        )

        items = _parse_rss(xml, limit)
        return items


def _parse_rss(xml: str, limit: int) -> list[GossipItem]:
    """Parse RSS XML into GossipItems."""
    items: list[GossipItem] = []
    item_blocks = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    for i, block in enumerate(item_blocks[:limit]):
        title_m = re.search(r"<title>(.*?)</title>", block)
        link_m = re.search(r"<link>(.*?)</link>", block)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title)
        title = unescape_html(title)
        # Skip the feed title
        if "Bing" in title or "热门" in title[:5]:
            continue
        url = link_m.group(1).strip() if link_m else ""
        items.append(
            GossipItem(
                platform="bing_news",
                rank=i + 1,
                title=title,
                url=url,
                heat=max(1, (len(item_blocks) - i) * 2000),
                tag=tag_from_title(title),
            )
        )
    return items


