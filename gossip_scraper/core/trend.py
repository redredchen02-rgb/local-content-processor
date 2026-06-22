"""Trend velocity tracking — detect rising/falling topics across runs.

Compares current rankings with the last run's rankings to compute velocity.
Rising topics (moving up in rank) get bonus points in the surprise dimension."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..models import GossipItem

_HISTORY_FILE = ".gossip_history.json"
_MAX_HISTORY_AGE = 3600  # 1 hour — stale data is useless

# Resolve once at import time so the history path never depends on the process cwd.
_DEFAULT_HISTORY_DIR = Path(__file__).resolve().parent.parent.parent


def compute_velocity(items: list[GossipItem], history_dir: Path | None = None) -> list[GossipItem]:
    """Compute trend velocity for each item based on rank changes.

    Velocity = (last_rank - current_rank) / time_delta
    Positive = rising, negative = falling, zero = new or unchanged."""
    if not items:
        return items

    hist_dir = history_dir or _DEFAULT_HISTORY_DIR
    history = _load_history(hist_dir)

    now = time.time()
    for it in items:
        key = _item_key(it)
        if key in history:
            prev_rank, prev_time = history[key]
            time_delta = max(now - prev_time, 1)
            it.trend_velocity = (prev_rank - it.rank) / time_delta
        else:
            # New topic — assign high velocity (it just appeared)
            it.trend_velocity = 0.5

    # Save current rankings for next run
    _save_history(items, hist_dir, now)

    return items


def _item_key(item: GossipItem) -> str:
    """Stable key for velocity tracking: title only (first 30 chars), no platform.

    After dedup the same topic appears once but its 'winning' platform can change
    between runs (e.g. weibo scraper fails → baidu wins), so including the platform
    in the key would silently reset velocity every time the dominant platform shifts.
    """
    return item.title[:30].lower()


def _load_history(hist_dir: Path) -> dict[str, tuple[int, float]]:
    """Load previous rankings: {key: (rank, timestamp)}."""
    path = hist_dir / _HISTORY_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Filter out stale entries; IndexError/TypeError guard malformed list values
        now = time.time()
        return {k: (v[0], v[1]) for k, v in data.items() if now - v[1] < _MAX_HISTORY_AGE}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def _save_history(items: list[GossipItem], hist_dir: Path, now: float) -> None:
    """Save current rankings for next run (atomic write via temp-file + rename)."""
    path = hist_dir / _HISTORY_FILE
    data = {_item_key(it): (it.rank, now) for it in items}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # POSIX-atomic; concurrent writers each win their own round
