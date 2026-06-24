"""RFI Chinese RSS scraper — French international radio in Chinese."""

from __future__ import annotations

from .base import fetch_text, parse_rss_items
from ..models import GossipItem

_RFI_RSS = "https://www.rfi.fr/cn/rss"


class RFIScraper:
    platform = "rfi"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(_RFI_RSS)
        return parse_rss_items(xml, self.platform, limit, base_heat=3000)
