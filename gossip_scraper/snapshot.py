"""Daily gossip snapshot — run once and save timestamped results.

Usage:
    python -m gossip_scraper.snapshot              # save today's snapshot
    python -m gossip_scraper.snapshot --compare    # compare with yesterday
    python -m gossip_scraper.snapshot --history    # show all snapshots
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .__main__ import DEFAULT_PLATFORMS, SCRAPERS
from .scrapers.base import ScraperProtocol

_SNAPSHOT_DIR = Path("gossip_history")


def save_snapshot() -> Path:
    """Run the scraper and save results to a timestamped JSON file."""
    _SNAPSHOT_DIR.mkdir(exist_ok=True)

    date_str = time.strftime("%Y-%m-%d")
    time_str = time.strftime("%H%M")
    filename = f"{date_str}_{time_str}.json"
    filepath = _SNAPSHOT_DIR / filename

    # Run the scraper
    import asyncio

    platforms = [p.strip() for p in DEFAULT_PLATFORMS.split(",") if p.strip()]
    items = asyncio.run(_fetch_all(platforms, limit=50))

    # Save
    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "date": date_str,
        "platforms": len(platforms),
        "total_items": len(items),
        "items": [it.to_dict() for it in items],
    }
    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {len(items)} items to {filepath}", file=sys.stderr)
    return filepath


async def _fetch_all(platforms: list[str], limit: int) -> list:
    """Fetch from all platforms."""
    import asyncio

    from .core.category import enrich_categories
    from .core.dedup import dedup
    from .core.geo import enrich_regions
    from .core.ranking import rank
    from .core.sentiment import enrich_sentiments
    from .core.summary import enrich_summaries
    from .core.trend import compute_velocity
    from .models import GossipItem

    scrapers: list[tuple[str, ScraperProtocol]] = []
    for name in platforms:
        cls = SCRAPERS.get(name)
        if cls:
            scrapers.append((name, cls()))

    async def _fetch_one(name: str, scraper: ScraperProtocol) -> list[GossipItem]:
        try:
            items = await scraper.fetch(limit=limit)
            print(f"  {name}: {len(items)}", file=sys.stderr)
            return items
        except Exception as e:
            print(f"  {name}: FAILED — {e}", file=sys.stderr)
            return []

    results = await asyncio.gather(*[_fetch_one(n, s) for n, s in scrapers])
    all_items = [it for batch in results for it in batch]
    all_items = dedup(all_items)
    all_items = enrich_regions(all_items)
    all_items = enrich_categories(all_items)
    all_items = compute_velocity(all_items)
    all_items = enrich_sentiments(all_items)
    all_items = enrich_summaries(all_items)
    all_items = rank(all_items)
    return all_items


def list_snapshots() -> list[Path]:
    """List all snapshot files sorted by date."""
    if not _SNAPSHOT_DIR.exists():
        return []
    return sorted(_SNAPSHOT_DIR.glob("*.json"))


def compare_snapshots(n: int = 2) -> None:
    """Compare the last n snapshots."""
    snapshots = list_snapshots()
    if len(snapshots) < 2:
        print("need at least 2 snapshots to compare", file=sys.stderr)
        return

    prev_path = snapshots[-2]
    curr_path = snapshots[-1]

    prev = json.loads(prev_path.read_text(encoding="utf-8"))
    curr = json.loads(curr_path.read_text(encoding="utf-8"))

    prev_titles = {it["title"] for it in prev["items"]}
    curr_titles = {it["title"] for it in curr["items"]}

    new_topics = curr_titles - prev_titles
    gone_topics = prev_titles - curr_titles
    stayed = curr_titles & prev_titles

    print(f"\n{'=' * 60}")
    print(f"  Comparison: {prev['date']} → {curr['date']}")
    print(f"{'=' * 60}")
    print(f"  Previous: {len(prev['items'])} items")
    print(f"  Current:  {len(curr['items'])} items")
    print(f"  New:      {len(new_topics)} topics")
    print(f"  Gone:     {len(gone_topics)} topics")
    print(f"  Stayed:   {len(stayed)} topics")
    print()

    if new_topics:
        print("  NEW topics:")
        for it in curr["items"]:
            if it["title"] in new_topics:
                print(f"    + [{it['category']}] {it['title'][:40]} (S={it['surprise_score']:.2f})")
        print()

    if gone_topics:
        print("  GONE topics:")
        for it in prev["items"]:
            if it["title"] in gone_topics:
                print(f"    - {it['title'][:40]}")
        print()


def show_history() -> None:
    """Show all snapshots with summary stats."""
    snapshots = list_snapshots()
    if not snapshots:
        print("no snapshots found", file=sys.stderr)
        return

    print(f"\n{'=' * 70}")
    print(f"  Gossip History — {len(snapshots)} snapshots")
    print(f"{'=' * 70}")
    for path in snapshots:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["items"]
        top3 = [it["title"][:25] for it in items[:3]]
        print(f"  {data['timestamp']}  |  {len(items):3d} items  |  {', '.join(top3)}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily gossip snapshot")
    parser.add_argument("--compare", action="store_true", help="compare with previous snapshot")
    parser.add_argument("--history", action="store_true", help="show all snapshots")
    args = parser.parse_args()

    if args.history:
        show_history()
    elif args.compare:
        compare_snapshots()
    else:
        save_snapshot()


if __name__ == "__main__":
    main()
