"""BBC Chinese RSS scraper — BBC news in simplified Chinese."""

from __future__ import annotations

import re

from .base import fetch_text, unescape_html, tag_from_title
from ..models import GossipItem

_BBC_RSS = "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml"


class BBCChineseScraper:
    platform = "bbc_chinese"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(_BBC_RSS)

        items = _parse_rss(xml, limit)
        return items


def _parse_rss(xml: str, limit: int) -> list[GossipItem]:
    """Parse RSS XML into GossipItems, handling CDATA wrappers."""
    items: list[GossipItem] = []
    item_blocks = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    for i, block in enumerate(item_blocks[:limit]):
        title_m = re.search(r"<title>(.*?)</title>", block)
        link_m = re.search(r"<link>(.*?)</link>", block)
        desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title)
        title = unescape_html(title)
        # Skip the channel title itself
        if title in ("BBC Chinese", "BBC 中文"):
            continue
        url = link_m.group(1).strip() if link_m else ""
        desc = ""
        if desc_m:
            desc = desc_m.group(1).strip()
            desc = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", desc)
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:200]
            desc = unescape_html(desc)
        items.append(
            GossipItem(
                platform="bbc_chinese",
                rank=i + 1,
                title=title,
                url=url,
                heat=max(1, (len(item_blocks) - i) * 4000),
                tag=tag_from_title(title),
                description=desc,
            )
        )
    return items


