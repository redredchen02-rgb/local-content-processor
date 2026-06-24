"""Base protocol + shared fetch helpers for gossip scrapers.

All scrapers must implement ``platform`` (a string identifier) and ``fetch``
(an async method returning a list of GossipItem). ``fetch_json`` / ``fetch_text``
eliminate the per-scraper hand-rolled httpx client + headers boilerplate."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..models import GossipItem

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


class ScraperProtocol(Protocol):
    """Protocol that all gossip scrapers must satisfy."""

    platform: str

    async def fetch(self, limit: int = 50) -> list[GossipItem]: ...


async def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 15,
    follow_redirects: bool = True,
) -> Any:
    """Fetch a URL and return parsed JSON."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        resp = await client.get(url, headers=merged, params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 15,
    follow_redirects: bool = True,
) -> str:
    """Fetch a URL and return response text."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        resp = await client.get(url, headers=merged, params=params)
        resp.raise_for_status()
        return resp.text
