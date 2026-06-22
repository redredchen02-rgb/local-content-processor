"""Netease hot news scraper — uses the mobile hot news API."""

from __future__ import annotations

import httpx

from ..models import GossipItem

_NETEASE_HOT = "https://m.163.com/fe/api/hot/news/flow"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://m.163.com",
}


class NeteaseScraper:
    platform = "netease"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _NETEASE_HOT,
                headers=_HEADERS,
                params={"api_version": "v1", "size": min(limit, 100)},
            )
            resp.raise_for_status()
            data = resp.json()

        items_list = data.get("data", {}).get("list", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(items_list[:limit]):
            title = entry.get("title", "")
            url = entry.get("source", "") or entry.get("url", "")
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=title,
                    url=url,
                    heat=max(1, (len(items_list) - i) * 5000),
                    tag=_tag_from_title(title),
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
