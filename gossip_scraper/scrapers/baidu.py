"""Baidu hot search scraper — extracts SSR data from the realtime board."""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

import httpx

from ..models import GossipItem

_BAIDU_HOT = "https://top.baidu.com/board?tab=realtime"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


class BaiduScraper:
    platform = "baidu"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_BAIDU_HOT, headers=_HEADERS)
            resp.raise_for_status()
            html = resp.text

        # Baidu embeds SSR data in an HTML comment: <!--s-data:{"data":{...}}-->
        m = re.search(r"<!--s-data:(.*?)-->", html, re.DOTALL)
        if not m:
            raise RuntimeError("Baidu hot: no SSR data found in HTML")

        data = json.loads(m.group(1))
        cards = data.get("data", {}).get("cards", [])
        if not cards:
            return []

        raw_items = cards[0].get("content", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(raw_items[:limit]):
            word = entry.get("word", "")
            heat = entry.get("hotScore", 0)
            hot_change = entry.get("hotChange", 0)
            try:
                hot_change = float(hot_change) if hot_change else 0.0
            except (ValueError, TypeError):
                hot_change = 0.0
            hot_tag = _tag_from_hot_tag(entry.get("hotTag", 0))
            desc = entry.get("desc", "")
            url = entry.get("url", f"https://www.baidu.com/s?wd={quote_plus(word)}")
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=word,
                    url=url,
                    heat=int(heat) if heat else 0,
                    hot_change=hot_change,
                    tag=hot_tag if hot_tag else _tag_from_desc(desc),
                )
            )
        return items


def _tag_from_hot_tag(tag: int | str) -> str:
    """Map Baidu's numeric hotTag to a Chinese tag."""
    try:
        tag = int(tag)
    except (ValueError, TypeError):
        tag = 0
    return {1: "新", 3: "热"}.get(tag, "")


def _tag_from_desc(desc: str) -> str:
    if not desc:
        return ""
    if "热" in desc:
        return "热"
    if "新" in desc:
        return "新"
    if "沸" in desc:
        return "沸"
    return ""
