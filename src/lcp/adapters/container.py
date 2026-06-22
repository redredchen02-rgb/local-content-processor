"""Typed adapter injection container for Pipeline.

Replaces the fixed ``Pipeline.__init__`` adapter parameters with a single,
typed container so new adapters register without touching every constructor/test.
The ``dry_run`` coercion/refusal (force-on or refuse) runs in ``Pipeline.__init__``
at construction time — this module is a data holder, not a policy enforcer.

Usage::

    adapters = Adapters(store=store, audit=audit, llm_client=client)
    pipeline = Pipeline(config, adapters, dry_run=False)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .crawler.base import CrawlerProtocol
from .llm.client import LlmClient
from .storage.audit_log import AuditLog
from .storage.job_store import JobStore


@dataclass(frozen=True)
class Adapters:
    """Typed injection container for pipeline adapters.

    ``crawler`` is optional (required only at ``stage1``). All other adapters
    are required. Adding a new adapter is a one-line field addition here plus
    wiring in ``Pipeline.__init__``.
    """

    store: JobStore
    audit: AuditLog
    llm_client: LlmClient
    crawler: CrawlerProtocol | None = None
