"""BBC Chinese RSS scraper — BBC news in simplified Chinese."""

from __future__ import annotations

from .base import fetch_text, parse_rss_items
from ..models import GossipItem

_BBC_RSS = "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml"
_SKIP = frozenset({"BBC Chinese", "BBC 中文"})


class BBCChineseScraper:
    platform = "bbc_chinese"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(_BBC_RSS)
        return parse_rss_items(xml, self.platform, limit, skip_titles=_SKIP)
