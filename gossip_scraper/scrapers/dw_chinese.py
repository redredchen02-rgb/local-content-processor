"""DW Chinese RSS scraper — Deutsche Welle news in Chinese."""

from __future__ import annotations

import re

from .base import fetch_text, unescape_html, tag_from_title
from ..models import GossipItem

_DW_RSS = "https://rss.dw.com/rdf/rss-chi-all"


class DWChineseScraper:
    platform = "dw_chinese"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        xml = await fetch_text(_DW_RSS)

        items = _parse_rdf(xml, limit)
        return items


def _parse_rdf(xml: str, limit: int) -> list[GossipItem]:
    """Parse RDF/RSS feed into GossipItems."""
    items: list[GossipItem] = []
    # RDF uses <item rdf:about="..."> with <title> and <link> inside
    item_blocks = re.findall(r"<item[^>]*>(.*?)</item>", xml, re.DOTALL)
    if not item_blocks:
        # Fallback: try standard RSS <item> pattern
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
        # Skip channel titles
        if "DW" in title or "Deutsche Welle" in title:
            continue
        url = link_m.group(1).strip() if link_m else ""
        desc = ""
        if desc_m:
            desc = desc_m.group(1).strip()
            desc = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", desc)
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:200]
        items.append(
            GossipItem(
                platform="dw_chinese",
                rank=i + 1,
                title=title,
                url=url,
                heat=max(1, (len(item_blocks) - i) * 3500),
                tag=tag_from_title(title),
                description=desc,
            )
        )
    return items


