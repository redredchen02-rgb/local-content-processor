"""Weibo hot search scraper — uses the public Ajax API."""

from __future__ import annotations

from urllib.parse import quote_plus

import httpx

from ..models import GossipItem

_WEIBO_API = "https://weibo.com/ajax/side/hotSearch"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://weibo.com",
}


class WeiboScraper:
    platform = "weibo"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_WEIBO_API, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()

        realtime = data.get("data", {}).get("realtime", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(realtime[:limit]):
            word = entry.get("word", "")
            raw_heat = entry.get("num", entry.get("raw_hot", 0))
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=word,
                    url=f"https://s.weibo.com/weibo?q={quote_plus(word)}",
                    heat=int(raw_heat) if raw_heat else 0,
                    tag=_tag_of(entry),
                )
            )
        return items


def _tag_of(entry: dict) -> str:
    """Extract the tag (hot/new/recommend/etc.) from a Weibo entry."""
    if entry.get("is_hot"):
        return "热"
    if entry.get("is_new"):
        return "新"
    if entry.get("is_fei"):
        return "沸"
    if entry.get("is_com"):
        return "荐"
    icon = entry.get("icon_desc", "")
    return icon if icon else ""
