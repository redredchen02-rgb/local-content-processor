"""Base protocol for gossip scrapers.

All scrapers must implement ``platform`` (a string identifier) and ``fetch``
(an async method returning a list of GossipItem). This replaces the duck-typed
``object`` that blocked strict mypy checking."""

from __future__ import annotations

from typing import Protocol

from ..models import GossipItem


class ScraperProtocol(Protocol):
    """Protocol that all gossip scrapers must satisfy."""

    platform: str

    async def fetch(self, limit: int = 50) -> list[GossipItem]: ...
