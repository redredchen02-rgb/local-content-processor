"""DW Chinese RSS scraper — Deutsche Welle news in Chinese."""

from __future__ import annotations

from ..models import GossipItem
from .base import fetch_text, parse_rss_items

_DW_RSS = "https://rss.dw.com/rdf/rss-chi-all"
_SKIP = frozenset({"DW", "Deutsche Welle"})


class DWChineseScraper:
    platform = "dw_chinese"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(_DW_RSS)
        items = parse_rss_items(xml, self.platform, limit, base_heat=3500)
        return [it for it in items if not any(s in it.title for s in _SKIP)]
