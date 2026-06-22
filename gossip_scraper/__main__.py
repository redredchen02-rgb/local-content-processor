"""Gossip scraper — aggregate trending drama from Chinese platforms.

Usage:
    python -m gossip_scraper                           # all platforms, top 30
    python -m gossip_scraper --platform weibo           # weibo only
    python -m gossip_scraper --top 10                   # top 10 after ranking
    python -m gossip_scraper --sort-by surprise         # sort by surprise score
    python -m gossip_scraper --no-dedup                 # skip cross-platform dedup
    python -m gossip_scraper --json                     # JSON to stdout
    python -m gossip_scraper -o 瓜.json                 # save to file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .core import health
from .core.dedup import dedup
from .core.ranking import rank
from .models import GossipItem
from .scrapers.baidu import BaiduScraper
from .scrapers.bilibili import BilibiliScraper
from .scrapers.bilibili_popular import BilibiliPopularScraper
from .scrapers.douban_celeb import DoubanCelebScraper
from .scrapers.douban_movie import DoubanMovieScraper
from .scrapers.douyin import DouyinScraper
from .scrapers.netease import NeteaseScraper
from .scrapers.sina import SinaScraper
from .scrapers.tieba import TiebaScraper
from .scrapers.toutiao import ToutiaoScraper
from .scrapers.weibo import WeiboScraper

SCRAPERS = {
    "weibo": WeiboScraper,
    "baidu": BaiduScraper,
    "bilibili": BilibiliScraper,
    "bilibili_popular": BilibiliPopularScraper,
    "douyin": DouyinScraper,
    "tieba": TiebaScraper,
    "toutiao": ToutiaoScraper,
    "douban_movie": DoubanMovieScraper,
    "douban_celeb": DoubanCelebScraper,
    "netease": NeteaseScraper,
    "sina": SinaScraper,
}

DEFAULT_PLATFORMS = "weibo,baidu,bilibili,douyin,tieba,toutiao,douban_movie,douban_celeb,netease,sina"


def _format_table(items: list[GossipItem], sort_by: str) -> str:
    """Pretty-print gossip items as a ranked table."""
    if not items:
        return "(empty)"
    lines = []
    max_title = max(len(it.title) for it in items)
    max_title = min(max_title, 50)
    for it in items:
        tag = f"[{it.tag}]" if it.tag else "    "
        heat = f"{it.heat:>10,}" if it.heat else ""
        title = it.title[:max_title]
        platforms = ",".join(it.merged_from) if it.merged_from else it.platform
        cross = f" ×{it.cross_platform_count}" if it.cross_platform_count > 1 else ""
        # Show dimension scores
        scores = (
            f"H:{it.heat_score:.2f} "
            f"F:{it.freshness_score:.2f} "
            f"S:{it.surprise_score:.2f}"
        )
        total = f"{it.score:.2f}"
        lines.append(
            f"  {it.rank:>3}. {tag} {title:<{max_title}}  "
            f"{heat:>10}  {scores}  ={total}  {platforms}{cross}"
        )
    return "\n".join(lines)


async def run(
    platforms: list[str],
    limit: int,
    top: int | None,
    sort_by: str,
    do_dedup: bool,
    output: str | None,
    as_json: bool,
) -> None:
    # --- Phase 1: Parallel fetch ---
    scrapers = []
    for name in platforms:
        cls = SCRAPERS.get(name)
        if cls is None:
            print(f"  unknown platform: {name}", file=sys.stderr)
            continue
        scrapers.append((name, cls()))

    async def _fetch_one(name: str, scraper: object) -> list[GossipItem]:
        try:
            items = await scraper.fetch(limit=limit)
            print(f"  {name}: {len(items)} items", file=sys.stderr)
            health.record(name, ok=True, item_count=len(items))
            return items
        except Exception as e:
            print(f"  {name}: FAILED — {e}", file=sys.stderr)
            health.record(name, ok=False, item_count=0)
            return []

    results = await asyncio.gather(*[_fetch_one(n, s) for n, s in scrapers])
    all_items = [it for batch in results for it in batch]

    # --- Phase 2: Dedup ---
    if do_dedup and len(platforms) > 1:
        before = len(all_items)
        all_items = dedup(all_items)
        merged = before - len(all_items)
        if merged:
            print(f"  dedup: merged {merged} duplicates", file=sys.stderr)

    # --- Phase 3: 3-dimension ranking ---
    all_items = rank(all_items, sort_by=sort_by)

    # --- Phase 4: Output ---
    if top:
        all_items = all_items[:top]

    if as_json:
        print(
            json.dumps(
                [it.to_dict() for it in all_items],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"\n{'='*105}")
        print(
            f"  Gossip Digest — {time.strftime('%Y-%m-%d %H:%M')}  "
            f"sort: {sort_by}"
        )
        print(
            f"  Platforms: {', '.join(platforms)}  |  "
            f"Total: {len(all_items)} items"
        )
        print(f"  H=Heat(流量) F=Fresh(新鮮) S=Surprise(反差)  total = 0.5H+0.25F+0.25S")
        print(f"{'='*105}")
        print(_format_table(all_items, sort_by))
        print()

    if output:
        path = Path(output)
        path.write_text(
            json.dumps(
                [it.to_dict() for it in all_items],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"saved to {path}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate trending gossip from Chinese platforms"
    )
    parser.add_argument(
        "--platform", "-p",
        default=DEFAULT_PLATFORMS,
        help=f"comma-separated platforms (default: {DEFAULT_PLATFORMS})",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=50,
        help="max items per platform (default: 50)",
    )
    parser.add_argument(
        "--top", "-t",
        type=int,
        default=None,
        help="show only top N items after ranking",
    )
    parser.add_argument(
        "--sort-by", "-s",
        choices=["score", "heat", "fresh", "surprise"],
        default="score",
        help="sort dimension (default: score = combined 3-dimension)",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="skip cross-platform deduplication",
    )
    parser.add_argument(
        "--output", "-o",
        help="save JSON to file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output JSON to stdout instead of table",
    )
    args = parser.parse_args()
    platforms = [p.strip() for p in args.platform.split(",") if p.strip()]
    asyncio.run(
        run(
            platforms,
            args.limit,
            args.top,
            args.sort_by,
            not args.no_dedup,
            args.output,
            args.json,
        )
    )


if __name__ == "__main__":
    main()
