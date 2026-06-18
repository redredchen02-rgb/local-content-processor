"""Boundary clock helper (U3): ISO8601 UTC 'now'."""

import datetime as dt

from lcp.adapters.clock import now


def test_now_is_iso8601_utc_z():
    s = now()
    assert s.endswith("Z")
    # round-trips through the exact documented format
    parsed = dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    assert parsed.year >= 2026
