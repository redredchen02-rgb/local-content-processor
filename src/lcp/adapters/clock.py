"""Boundary clock: ISO8601 UTC 'now' for the shells.

Wall-clock time is nondeterministic (effectively I/O), so it lives in an adapter,
not the pure core. Both shells previously carried a byte-identical ``_now()``;
this is the single copy (plan 004 U3)."""

from __future__ import annotations

import datetime as _dt


def now() -> str:
    """ISO8601 UTC timestamp, e.g. ``2026-06-17T12:00:00Z``.

    The shells mint the boundary timestamp here so the lower layers
    (core/adapters logic) stay deterministic."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
