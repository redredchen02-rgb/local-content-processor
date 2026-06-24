"""RFI Chinese RSS scraper — French international radio in Chinese."""

from __future__ import annotations

import re

from .base import fetch_text, unescape_html
from ..models import GossipItem

_RFI_RSS = "https://www.rfi.fr/cn/rss"


class RFIScraper:
    platform = "rfi"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(_RFI_RSS)

        items = _parse_rss(xml, limit)
        return items


def _parse_rss(xml: str, limit: int) -> list[GossipItem]:
    """Parse RSS XML into GossipItems."""
    items: list[GossipItem] = []
    # Match <item> blocks
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
        url = link_m.group(1).strip() if link_m else ""
        desc = ""
        if desc_m:
            desc = desc_m.group(1).strip()
            desc = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", desc)
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:200]
        items.append(
            GossipItem(
                platform="rfi",
                rank=i + 1,
                title=title,
                url=url,
                heat=max(1, (len(item_blocks) - i) * 3000),
                tag=_tag_from_title(title),
                description=desc,
            )
        )
    return items


def _tag_from_title(title: str) -> str:
    if "突发" in title or "刚刚" in title:
        return "突发"
    if "独家" in title:
        return "独家"
    if "热" in title:
        return "热"
    return ""
