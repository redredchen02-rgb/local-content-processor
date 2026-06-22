"""Per-platform scraper health monitoring (R15).

Appends one JSONL record per platform per run; when a platform's success rate
over the trailing 7-day window drops below the threshold (and there is enough
data to judge), emits a single ERROR log line naming the platform and rate.

This is a log line, not an actuator: a degraded scraper keeps feeding the
pipeline until a human reads the log. That is acceptable on the manual-review
path; revisit if/when auto-publish ships."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_LOG = logging.getLogger("gossip_scraper.health")

_DEFAULT_PATH = Path.home() / ".gossip_scraper" / "health.jsonl"
_WINDOW_SECONDS = 7 * 24 * 3600
_THRESHOLD = 0.60
_MIN_SAMPLES = 3  # below this many records in the window, don't alarm (insufficient data)
_MAX_RECORDS = 5000  # keep the file bounded


def record(
    platform: str,
    ok: bool,
    item_count: int,
    *,
    path: Path | str | None = None,
    now: float | None = None,
) -> float | None:
    """Append a run result for `platform` and, if its trailing-window success
    rate is below threshold with enough samples, emit one ERROR log line.

    Returns the computed rolling success rate (or None if insufficient data)."""
    p = Path(path) if path is not None else _DEFAULT_PATH
    t = now if now is not None else time.time()
    _append(p, {"ts": t, "platform": platform, "ok": bool(ok), "n": int(item_count)})
    rate = _rolling_rate(p, platform, t)
    if rate is not None and rate < _THRESHOLD:
        _LOG.error(
            "scraper '%s' 7-day success rate %.0f%% < %.0f%% — investigate",
            platform,
            rate * 100,
            _THRESHOLD * 100,
        )
    return rate


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _truncate(path)


def _truncate(path: Path) -> None:
    """Keep only the most recent _MAX_RECORDS lines (bounded file)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > _MAX_RECORDS:
        path.write_text("\n".join(lines[-_MAX_RECORDS:]) + "\n", encoding="utf-8")


def _rolling_rate(path: Path, platform: str, now: float) -> float | None:
    """Success fraction for `platform` over the trailing window, or None if there
    are fewer than _MIN_SAMPLES records in the window (insufficient data)."""
    cutoff = now - _WINDOW_SECONDS
    oks = 0
    total = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("platform") != platform:
            continue
        ts = rec.get("ts", 0)
        if ts < cutoff or ts > now:
            continue
        total += 1
        if rec.get("ok"):
            oks += 1
    if total < _MIN_SAMPLES:
        return None
    return oks / total
