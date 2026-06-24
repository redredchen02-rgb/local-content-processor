"""Base protocol + shared fetch helpers for gossip scrapers.

All scrapers must implement ``platform`` (a string identifier) and ``fetch``
(an async method returning a list of GossipItem). ``fetch_json`` / ``fetch_text``
eliminate the per-scraper hand-rolled httpx client + headers boilerplate."""

from __future__ import annotations

import asyncio
import html as _html
from typing import Any, Protocol

import httpx

from ..models import GossipItem

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

_MAX_RETRIES = 2
_RETRY_BACKOFF = [0.5, 1.0]


def _is_transient(exc: Exception) -> bool:
    """True for errors worth retrying (timeout, 429, 5xx)."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


class ScraperProtocol(Protocol):
    """Protocol that all gossip scrapers must satisfy."""

    platform: str

    async def fetch(self, limit: int = 50) -> list[GossipItem]: ...


async def _fetch_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None,
    timeout: int,
    follow_redirects: bool,
    as_json: bool,
) -> Any:
    """Fetch with transient-error retry. Raises non-transient errors immediately."""
    merged = {**DEFAULT_HEADERS, **headers}
    last_exc: Exception | None = None
    for attempt in range(1 + _MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as c:
                resp = await c.get(url, headers=merged, params=params)
                resp.raise_for_status()
                return resp.json() if as_json else resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES and _is_transient(exc):
                await asyncio.sleep(_RETRY_BACKOFF[attempt])
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 15,
    follow_redirects: bool = True,
) -> Any:
    """Fetch a URL and return parsed JSON. Retries on transient errors."""
    return await _fetch_with_retry(
        url, headers=headers or {}, params=params,
        timeout=timeout, follow_redirects=follow_redirects, as_json=True,
    )


async def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 15,
    follow_redirects: bool = True,
) -> str:
    """Fetch a URL and return response text. Retries on transient errors."""
    return await _fetch_with_retry(
        url, headers=headers or {}, params=params,
        timeout=timeout, follow_redirects=follow_redirects, as_json=False,
    )


def unescape_html(text: str) -> str:
    """Unescape HTML entities (&amp; → &, &lt; → <, etc.)."""
    return _html.unescape(text)


def tag_from_title(title: str) -> str:
    """Extract a short tag from a Chinese news title. Shared across scrapers."""
    if "突发" in title or "刚刚" in title:
        return "突发"
    if "独家" in title:
        return "独家"
    if "热" in title:
        return "热"
    return ""
