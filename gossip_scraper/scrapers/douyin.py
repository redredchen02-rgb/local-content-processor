"""Douyin (抖音) hot-search scraper — uses the public hot-search billboard API.

Douyin web is a JS SPA, but the billboard word API returns plain JSON over HTTP
(no JS execution needed). On any network/parse failure the scraper RAISES (the
same convention as the weibo/baidu/toutiao scrapers): `run()._fetch_one` catches
it, so a Douyin outage is a clean per-platform miss recorded by the health
monitor, never a batch failure. Feasibility against the live endpoint is
best-effort (plan: deferred) — if the shape drifts, the health monitor surfaces
it as a sustained failure."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from ..models import GossipItem

_DOUYIN_HOT = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.douyin.com",
}


class DouyinScraper:
    platform = "douyin"

    async def fetch(self, limit: int = 50) -> list[GossipItem]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_DOUYIN_HOT, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()

        word_list = data.get("word_list", [])
        items: list[GossipItem] = []
        for i, entry in enumerate(word_list[:limit]):
            word = entry.get("word", "")
            hot_value = entry.get("hot_value", 0)
            items.append(
                GossipItem(
                    platform=self.platform,
                    rank=i + 1,
                    title=word,
                    # Hot-search words resolve to a Douyin search page (a
                    # topic/aggregation page, like Weibo) — not a single article.
                    url=f"https://www.douyin.com/search/{quote(word, safe='')}",
                    heat=int(hot_value) if hot_value else 0,
                    tag=_tag_from_label(entry.get("label", 0)),
                )
            )
        return items


def _tag_from_label(label: int | str) -> str:
    """Map Douyin's numeric billboard label to a short Chinese tag.

    Douyin billboard labels: 1=新, 3=热, 8=爆 (the widely-used hot-board
    convention); anything else is untagged."""
    try:
        label = int(label)
    except (ValueError, TypeError):
        label = 0
    return {1: "新", 3: "热", 8: "爆"}.get(label, "")
