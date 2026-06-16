"""Domain allowlist with a recorded legal-basis per source (plan R6, R7).

The allowlist is the origin's only product blocker: we crawl ONLY public,
legally-citable sources, and we record WHY each is permitted (legal_basis) so
the decision is auditable. `is_allowed` is pure (no I/O); the loader maps
config.crawler.allow_domains into entries.

Matching: exact host or a subdomain of an allowed domain (e.g. allow
"example.com" -> "news.example.com" matches, "notexample.com" does NOT).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceEntry:
    """One allowlisted domain plus the legal basis for crawling it."""

    domain: str
    legal_basis: str = "unspecified"


def _normalize(domain: str) -> str:
    return domain.strip().lower().rstrip(".")


def _host_matches(host: str, domain: str) -> bool:
    host = _normalize(host)
    domain = _normalize(domain)
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


class SourceRegistry:
    """Allowlist of crawlable domains, each with a recorded legal basis."""

    def __init__(self, entries: list[SourceEntry] | None = None):
        self._entries: list[SourceEntry] = list(entries or [])

    @classmethod
    def from_config(cls, crawler_config: Any) -> "SourceRegistry":
        """Build from a CrawlerConfig. allow_domains may be plain strings or
        "domain|legal basis" pairs (pipe-separated) so the basis can live in
        config.yaml without a schema change."""
        entries: list[SourceEntry] = []
        for raw in getattr(crawler_config, "allow_domains", []) or []:
            if "|" in raw:
                domain, basis = raw.split("|", 1)
                entries.append(SourceEntry(domain=domain.strip(), legal_basis=basis.strip()))
            else:
                entries.append(SourceEntry(domain=raw.strip()))
        return cls(entries)

    @property
    def domains(self) -> list[str]:
        return [_normalize(e.domain) for e in self._entries]

    def is_allowed(self, host: str) -> bool:
        """Pure predicate: True iff `host` is (a subdomain of) an allowed domain."""
        return any(_host_matches(host, e.domain) for e in self._entries)

    def entry_for(self, host: str) -> SourceEntry | None:
        for e in self._entries:
            if _host_matches(host, e.domain):
                return e
        return None

    def legal_basis_for(self, host: str) -> str | None:
        e = self.entry_for(host)
        return e.legal_basis if e else None
