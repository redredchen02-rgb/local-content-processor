"""PTT Gossiping board scraper — HTML scrape with over18 cookie."""

from __future__ import annotations

import re

from .base import fetch_text
from ..models import GossipItem

_PTT_GOSSIP = "https://www.ptt.cc/bbs/Gossiping/index.html"
_EXTRA_HEADERS = {
    "Cookie": "over18=1",
}


class PTTScraper:
    platform = "ptt"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        html = await fetch_text(_PTT_GOSSIP, headers=_EXTRA_HEADERS)

        # Parse titles and URLs from the board page
        pattern = r'<div class="title">\s*<a href="(/bbs/Gossiping/[^"]+)">([^<]+)</a>'
        matches = re.findall(pattern, html)

        items: list[GossipItem] = []
        for i, (href, title) in enumerate(matches[:limit]):
            title = title.strip()
            tag = _tag_from_title(title)
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=title,
                    url=f"https://www.ptt.cc{href}",
                    heat=max(1, (len(matches) - i) * 500),
                    tag=tag,
                )
            )
        return items


def _tag_from_title(title: str) -> str:
    if title.startswith("[爆卦]"):
        return "爆"
    if title.startswith("[問卦]"):
        return "問"
    if title.startswith("[新聞]"):
        return "新聞"
    if title.startswith("[請益]"):
        return "請益"
    if "Re:" in title:
        return "推"
    return ""
