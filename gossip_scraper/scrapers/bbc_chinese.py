"""BBC Chinese RSS scraper — BBC news in simplified Chinese."""

from __future__ import annotations

import re

import httpx

from ..models import GossipItem

_BBC_RSS = "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


class BBCChineseScraper:
    platform = "bbc_chinese"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_BBC_RSS, headers=_HEADERS)
            resp.raise_for_status()
            xml = resp.text

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
        # Skip the channel title itself
        if title in ("BBC Chinese", "BBC 中文"):
            continue
        url = link_m.group(1).strip() if link_m else ""
        desc = ""
        if desc_m:
            desc = desc_m.group(1).strip()
            desc = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", desc)
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:200]
        items.append(
            GossipItem(
                platform="bbc_chinese",
                rank=i + 1,
                title=title,
                url=url,
                heat=max(1, (len(item_blocks) - i) * 4000),
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
