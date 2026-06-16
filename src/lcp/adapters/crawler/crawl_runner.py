"""Per-job crawl orchestration: subprocess-per-job + guards + audit (Unit 4).

Runs the Scrapy spider in a SUBPROCESS per job
(`subprocess.run([sys.executable,"-m",<module>,...], timeout=, env=minimal_env())`)
to dodge ReactorNotRestartable and isolate crawler crashes. umask is already
0077 at startup so spawned downloads land 0600.

Pre-flight (in-parent, before spawn) is the security gate:
1. allowlist check (SourceRegistry.is_allowed) — reject + audit if off-list,
2. SSRF validation (net_guard.validate_url) — reject + audit on non-global IP.

After the subprocess returns, the runner reads the manifest the child wrote and
returns a RawJobBundle. The subprocess receives a SCRUBBED env (minimal_env)
so it cannot inherit secrets like LCP_LLM_API_KEY (plan R40/R44).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Callable
from urllib.parse import urlsplit

from ...core.errors import ExternalServiceError, InputValidationError
from ...core.models import SourceType
from ...runtime_hardening import minimal_env
from ..storage.audit_log import AuditLog
from ..storage.manifest import read_manifest
from . import net_guard
from .base import RawJobBundle, SourceSpec
from .source_registry import SourceRegistry

SCRAPY_MODULE = "lcp.adapters.crawler.scrapy_impl"

EVENT_CRAWL_REJECTED = "CRAWL_REJECTED"
EVENT_CRAWL_DONE = "CRAWL_DONE"


def _host_of(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise InputValidationError(f"URL has no host: {url!r}")
    return host


class CrawlRunner:
    """Orchestrates a single URL crawl in an isolated subprocess."""

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        timeout: int = 30,
        audit: AuditLog | None = None,
        actor: str = "crawler",
        python_executable: str | None = None,
        subprocess_runner=subprocess.run,
        resolver=None,
    ):
        self.registry = registry
        self.timeout = timeout
        self.audit = audit
        self.actor = actor
        self.python = python_executable or sys.executable
        self._run = subprocess_runner  # injectable for tests
        self._resolver = resolver      # injectable DNS resolver for tests

    # --- pre-flight guards (pure-ish, parent process) ---

    def preflight(self, url: str, *, ts: str) -> str:
        """Allowlist + SSRF checks. Returns the validated host. Raises (and
        audits) InputValidationError if rejected."""
        host = _host_of(url)
        if not self.registry.is_allowed(host):
            self._audit_reject(ts, "domain_not_allowlisted")
            raise InputValidationError(f"domain not in allowlist: {host}")
        try:
            net_guard.validate_url(url, resolver=self._resolver)
        except InputValidationError:
            self._audit_reject(ts, "ssrf_blocked")
            raise
        return host

    def _audit_reject(self, ts: str, reason: str) -> None:
        if self.audit is not None:
            self.audit.append(
                ts=ts,
                stage="crawl",
                event=EVENT_CRAWL_REJECTED,
                job_id="-",
                actor=self.actor,
                extra={"reason": reason},
            )

    # --- main entry ---

    def crawl_url(self, spec: SourceSpec, *, ts: str) -> RawJobBundle:
        """Run one URL crawl: pre-flight guards, spawn the Scrapy subprocess
        with a scrubbed env + timeout, then read back the bundle the child
        wrote. Raises on rejection; raises ExternalServiceError on subprocess
        failure (retriable)."""
        if spec.source_type not in (SourceType.URL, SourceType.URL_LIST) or not spec.url:
            raise InputValidationError("crawl_url requires a URL spec")

        host = self.preflight(spec.url, ts=ts)
        spec.job_dir.mkdir(parents=True, exist_ok=True)
        (spec.job_dir / "raw").mkdir(parents=True, exist_ok=True)

        legal_basis = self.registry.legal_basis_for(host)
        cmd = [
            self.python, "-m", SCRAPY_MODULE,
            "--url", spec.url,
            "--job-id", spec.job_id,
            "--job-dir", str(spec.job_dir),
            "--timeout", str(self.timeout),
            "--source-domain", host,
            "--fetched-at", ts,
        ]
        for d in self.registry.domains:
            cmd += ["--allow-domain", d]

        try:
            proc = self._run(
                cmd,
                timeout=self.timeout + 30,   # outer guard > Scrapy's own timeout
                env=minimal_env(),           # NO secrets inherited (R40/R44)
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            raise ExternalServiceError(f"crawl subprocess timed out: {e}") from e

        manifest = read_manifest(spec.job_dir)
        if manifest is None:
            raise ExternalServiceError(
                f"crawl subprocess produced no manifest (rc={getattr(proc,'returncode',None)})"
            )

        if self.audit is not None:
            self.audit.append(
                ts=ts,
                stage="crawl",
                event=EVENT_CRAWL_DONE,
                job_id=spec.job_id,
                actor=self.actor,
                extra={"status": manifest.crawl_status, "legal_basis": legal_basis or "unspecified"},
            )

        return RawJobBundle(
            job_id=spec.job_id,
            raw_dir=spec.job_dir / "raw",
            manifest=manifest,
            job_status=manifest.crawl_status,
        )


class CrawlRunnerCrawler:
    """Adapt the URL :class:`CrawlRunner` (whose ``crawl_url`` needs a boundary
    timestamp) to the :class:`Crawler` contract (``crawl(spec) -> RawJobBundle``)
    so BOTH shells can drive the network crawl through ``Pipeline.stage1`` exactly
    like ingest — no shell re-implements Stage 1.

    The ``ts_provider`` is the shell's boundary timestamp factory (``cli._now`` /
    ``gui._now``), so the crawl's audit events still get a boundary-minted ts and
    the lower layers stay deterministic. Lives here (not in a shell) so the GUI
    and CLI share one adapter instead of each owning a copy."""

    def __init__(self, runner: CrawlRunner, *, ts_provider: Callable[[], str]):
        self._runner = runner
        self._ts = ts_provider

    def crawl(self, spec: SourceSpec) -> RawJobBundle:
        return self._runner.crawl_url(spec, ts=self._ts())
