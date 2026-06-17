"""Minimal GUI (Unit 9) — a pywebview js_api thin shell over the SAME core the
CLI uses (CLI/GUI parity, plan G6/G7).

WHY THIS FILE IS TESTABLE WITHOUT A WINDOW
==========================================
All operator logic lives in :class:`Api` — a plain object with NO pywebview
dependency. Tests import ``Api`` and call its methods directly; no window, no
HTTP server, no event loop. ``import webview`` happens LAZILY inside
:func:`launch` only, so importing this module (and exercising ``Api``) works
headless. ``Api`` is the GUI's I/O boundary, so — exactly like ``cli.Ctx`` /
``cli._now()`` — it builds config + adapters per call and generates the ISO8601
timestamp here (keeping core/adapters deterministic).

THE THREE LETHAL-TRIFECTA LEGS STAY CLOSED (plan redline 3 / R41)
================================================================
The js_api bridge is the place a webview XSS could turn into read/write/network
against core, so the output boundary is hardened on BOTH ends:

  * Every ``Api`` method returns a JSON-able dict whose attacker-shapeable fields
    are already escaped via :mod:`sanitizer` (``sanitize_draft`` / ``escape_html``
    / ``inert_link``). Source URLs come back as INERT text, never an ``<a href>``,
    never fetched. ``app.js`` renders these with ``textContent`` (never
    ``innerHTML``), and ``index.html`` carries a strict CSP with ``img-src
    'self'``.
  * Errors do NOT cross the bridge as exceptions (which could leak a stack /
    secret). Each method catches :class:`LcpError` and returns a small
    ``{"error": ..., "exit_code": ...}`` dict instead.

LOAD MODEL (feasibility-corrected, plan line 528)
=================================================
:func:`launch` serves the ``web/`` directory via pywebview's built-in HTTP
server bound to ``127.0.0.1`` ONLY (loopback; off-host unreachable — kills
network CSRF/CORS/DNS-rebinding). We do NOT use inline ``html=`` (its serverless
mode cannot load the external ``app.js`` / ``cover.jpg`` and would clash with the
no-inline CSP). Loopback-only stops a *network* attacker; it does not make DOM
content trusted — which is exactly why the R41 output escaping above is the real
defence.

CONCURRENCY: each handler opens its OWN JobStore connection (WAL-safe) by
rebuilding the per-call context, so background threads (crawl/process) never
share a SQLite handle. The GUI polls state via :meth:`Api.job_status` /
:meth:`Api.list_jobs` / :meth:`Api.summary`.
"""

from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path

from . import pipeline as pl
from .core import config as _config
from .adapters.crawler.base import SourceSpec
from .adapters.crawler.crawl_runner import CrawlRunner, CrawlRunnerCrawler
from .adapters.crawler.ingest import LocalIngestCrawler
from .adapters.crawler.source_registry import SourceRegistry
from .adapters.processor.sanitizer import escape_html, inert_link, sanitize_draft
from .adapters.publisher import signoff
from .adapters.publisher.review_packet import build_review_packet
from .adapters.storage.audit_aggregate import aggregate_audit, summarize_gaps
from .adapters.storage.audit_log import AuditLog
from .adapters.storage.job_store import JobStore
from .adapters.storage.source_store import SourceStore
from .core.config import load_config
from .core.errors import EXIT_INTERNAL, LcpError
from .core.models import SourceType

# The web/ assets directory served by pywebview's 127.0.0.1-only HTTP server.
WEB_DIR = Path(__file__).resolve().parent / "web"

# pywebview's built-in HTTP server binds to this loopback address BY DEFAULT
# (its server.address is hard-coded to 127.0.0.1 and bottle's default host is
# loopback) — never the all-interfaces wildcard. There is NO supported `host`
# argument to webview.start() (passing one raises TypeError), so we rely on and
# document that loopback default. This constant records the expected bind
# address; a test asserts launch() never passes an unsupported start() kwarg.
SERVER_HOST = "127.0.0.1"


def _now() -> str:
    """ISO8601 UTC timestamp. Api is the GUI's I/O boundary, so it mints the
    timestamp here (mirrors cli._now()), keeping core/adapters deterministic."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_dict(err: LcpError) -> dict:
    """Map an LcpError to a bridge-safe dict (no stack, no secrets leaked).

    The message is escaped too — error strings can echo attacker-shapeable input
    (e.g. a bad job id), so we never hand the GUI a raw string to render."""
    return {"error": escape_html(str(err)), "exit_code": getattr(err, "exit_code", EXIT_INTERNAL)}


class _Ctx:
    """Per-call context: config + freshly-opened adapters, built from the config
    path. Mirrors cli.Ctx so the GUI and CLI share identical wiring. Each call
    opens its OWN JobStore connection (WAL-safe across background threads)."""

    def __init__(self, config_path: str | None, base_dir: str | None):
        # Lenient: the GUI MANAGES its own config file (Settings panel), so a
        # path that does not exist yet falls back to defaults rather than raising
        # — but a present-but-invalid file still surfaces its error. An explicit
        # path is never silently ignored once the file exists.
        load_path = config_path if (config_path and Path(config_path).exists()) else None
        self.config = load_config(load_path)
        resolved = base_dir or self.config.storage.base_dir
        self.store = JobStore(base_dir=resolved)
        self.audit = AuditLog(Path(resolved) / "audit.jsonl")
        self.sources = SourceStore(base_dir=resolved)


class Api:
    """js_api bridge: one method per operator action, mirroring the CLI 1:1.

    Each method: parse args -> call pipeline/publisher/signoff via a fresh _Ctx
    (its own JobStore connection) -> return a JSON-able dict of SANITIZED data.
    LcpError is caught and returned as {"error", "exit_code"} rather than thrown
    across the bridge."""

    def __init__(self, config_path: str | None = None, base_dir: str | None = None):
        # Stored, not resolved — we rebuild a _Ctx (and a fresh JobStore) per
        # call so concurrent handlers never share a SQLite handle.
        self._config_path = config_path
        self._base_dir = base_dir
        # Background job status, keyed by job_id (in-memory, polled by the GUI).
        self._status: dict[str, dict] = {}
        self._status_lock = threading.Lock()

    def _ctx(self) -> _Ctx:
        return _Ctx(self._config_path, self._base_dir)

    # --- Stage 1: create + crawl / ingest ------------------------------------

    def create_and_crawl(self, job_id: str, url: str) -> dict:
        """Mirror `crawl`: create + crawl a URL into a raw bundle through the SAME
        Pipeline.stage1 the CLI and ingest use (no hand-rolled Stage 1, no private
        status->state map). Returns the resting state. SSRF/allowlist is enforced
        by the runner's preflight (we never resolve the URL here)."""
        try:
            c = self._ctx()
            registry = SourceRegistry.from_config(c.config.crawler)
            crawler = CrawlRunnerCrawler(
                CrawlRunner(
                    registry, timeout=c.config.crawler.timeout_seconds, audit=c.audit
                ),
                ts_provider=_now,
            )
            spec = SourceSpec(
                job_id=job_id,
                source_type=SourceType.URL,
                job_dir=c.store.job_dir(job_id),
                url=url,
                max_assets=c.config.crawler.max_assets_per_job,
            )
            p = pl.Pipeline(c.config, c.store, c.audit, crawler=crawler)
            res = p.stage1(spec, ts=_now())
            return {
                "job_id": escape_html(job_id),
                "crawl_status": escape_html(res.crawl_status),
                "state": res.record.state.value,
            }
        except LcpError as e:
            return _error_dict(e)

    def ingest_dir(self, job_id: str, directory: str) -> dict:
        """Mirror `ingest`: ingest a local material folder (no network)."""
        try:
            c = self._ctx()
            ts = _now()
            crawler = LocalIngestCrawler()
            p = pl.Pipeline(c.config, c.store, c.audit, crawler=crawler)
            spec = SourceSpec(
                job_id=job_id,
                source_type=SourceType.LOCAL_DIR,
                job_dir=c.store.job_dir(job_id),
                local_dir=Path(directory),
                max_assets=c.config.crawler.max_assets_per_job,
            )
            rec = p.stage1(spec, ts=ts).record
            return {"job_id": escape_html(job_id), "state": rec.state.value}
        except LcpError as e:
            return _error_dict(e)

    # --- Stage 2: process ----------------------------------------------------

    def templates(self) -> dict:
        """List the per-栏目 template categories the operator can apply (Unit 5).
        Names only — the template bodies never cross the bridge."""
        try:
            from .adapters.llm import templates as tmpl

            c = self._ctx()
            return {"categories": [escape_html(x) for x in tmpl.list_template_categories(c.config)]}
        except LcpError as e:
            return _error_dict(e)

    def process(
        self,
        job_id: str,
        title: str = "",
        dry_run: bool = False,
        watermark: bool | None = None,
        template: str | None = None,
        ai_copy: bool = False,
    ) -> dict:
        """Mirror `process`: risk + dedup gates -> assemble -> lint + ground.

        Honours dry_run (LLM not called). Stops at the first gate that parks the
        job and reports the resting state. watermark/template/ai_copy are
        process-time inputs (1:1 with the CLI flags)."""
        try:
            c = self._ctx()
            p = pl.Pipeline(c.config, c.store, c.audit, dry_run=bool(dry_run))
            res = p.process(
                job_id, ts=_now(), title=title,
                watermark=watermark,
                template=template or None,
                ai_copy=bool(ai_copy),
            )
            return {
                "job_id": escape_html(job_id),
                "state": res.final_state.value,
                "stopped_at": escape_html(res.stopped_at) if res.stopped_at else None,
                "dry_run": res.dry_run,
                "notes": [escape_html(n) for n in res.notes],
            }
        except LcpError as e:
            return _error_dict(e)

    # --- Long tasks: background thread + polled status -----------------------

    def _run_bg(self, job_id: str, fn) -> dict:
        """Run a long task (crawl/process) in a background thread; the GUI polls
        :meth:`job_status` for completion. Returns immediately with 'running'."""
        with self._status_lock:
            self._status[job_id] = {"job_id": escape_html(job_id), "status": "running"}

        def _worker():
            try:
                result = fn()
            except Exception:  # noqa: BLE001 - background-thread boundary
                # A non-LcpError must NOT kill the worker and strand status at
                # "running" forever. Return the same bridge-safe error shape fn()
                # itself would (no raw exception text crosses the bridge).
                result = {"error": "internal error", "exit_code": EXIT_INTERNAL}
            done = "error" if "error" in result else "done"
            with self._status_lock:
                self._status[job_id] = {
                    "job_id": escape_html(job_id),
                    "status": done,
                    "result": result,
                }

        threading.Thread(target=_worker, daemon=True).start()
        return {"job_id": escape_html(job_id), "status": "running"}

    def create_and_crawl_async(self, job_id: str, url: str) -> dict:
        """Background variant of create_and_crawl (long network task)."""
        return self._run_bg(job_id, lambda: self.create_and_crawl(job_id, url))

    def process_async(
        self,
        job_id: str,
        title: str = "",
        dry_run: bool = False,
        watermark: bool | None = None,
        template: str | None = None,
        ai_copy: bool = False,
    ) -> dict:
        """Background variant of process (long LLM task)."""
        return self._run_bg(
            job_id,
            lambda: self.process(job_id, title, dry_run, watermark, template, ai_copy),
        )

    def job_status(self, job_id: str) -> dict:
        """Read a background task's status: running | done | error | unknown.

        The persisted state (list_jobs/summary) is the source of truth; this is
        just the in-memory progress of an in-flight background task."""
        with self._status_lock:
            st = self._status.get(job_id)
        if st is not None:
            return st
        # No background task seen — fall back to the persisted record's state.
        try:
            c = self._ctx()
            rec = c.store.get_job(job_id)
            if rec is None:
                return {"job_id": escape_html(job_id), "status": "unknown"}
            return {"job_id": escape_html(job_id), "status": "idle", "state": rec.state.value}
        except LcpError as e:
            return _error_dict(e)

    # --- Review packet (freeze) + sign-off -----------------------------------

    def make_review_packet(self, job_id: str) -> dict:
        """Mirror `review-packet`: freeze the persisted Stage-2 draft (PROCESSED
        -> REVIEW_PENDING). Human action, not auto."""
        try:
            c = self._ctx()
            draft = pl.load_draft(c.store, job_id)
            if draft is None:
                return _error_dict(
                    _input_error(
                        f"no processed draft for {job_id}; run process first"
                    )
                )
            packet = build_review_packet(
                job_id=job_id,
                draft=draft,
                store=c.store,
                audit=c.audit,
                submitted_at=_now(),
                source_urls=[],
            )
            return {
                "job_id": escape_html(job_id),
                "state": "review_pending",
                "body_sha256": packet.body_sha256,
                "title_sha256": packet.title_sha256,
                "cover_sha256": packet.cover_sha256,
            }
        except LcpError as e:
            return _error_dict(e)

    def get_packet(self, job_id: str) -> dict:
        """Return the SANITIZED draft for display (every attacker-shapeable field
        already escaped; source URLs inert). The GUI renders this dict with
        textContent — never innerHTML."""
        try:
            c = self._ctx()
            draft = pl.load_draft(c.store, job_id)
            if draft is None:
                return _error_dict(
                    _input_error(f"no draft for {job_id}")
                )
            rec = c.store.get_job(job_id)
            sanitized = sanitize_draft(draft, source_urls=[])
            sanitized["job_id"] = escape_html(job_id)
            sanitized["state"] = rec.state.value if rec else None
            return sanitized
        except LcpError as e:
            return _error_dict(e)

    def cover_report(self, job_id: str) -> dict:
        """Cover safe-area advisories + preview/cover paths from the media gate's
        validation report (Unit 5). Advisory text only — never a hard gate. All
        strings are escaped; paths are inert text (the GUI shows them, the
        loopback server does not serve job dirs, so no <img> is embedded)."""
        try:
            import json

            c = self._ctx()
            report_path = c.store.job_dir(job_id) / "processed" / "validation_report.json"
            if not report_path.exists():
                return {"job_id": escape_html(job_id), "has_report": False}
            data = json.loads(report_path.read_text(encoding="utf-8"))
            adv = data.get("cover_advisories") or {}
            return {
                "job_id": escape_html(job_id),
                "has_report": True,
                "cover": escape_html(data["cover"]) if data.get("cover") else None,
                "cover_preview": (
                    escape_html(data["cover_preview"]) if data.get("cover_preview") else None
                ),
                "geometry": [escape_html(g) for g in adv.get("geometry", [])],
                "aesthetic": [escape_html(a) for a in adv.get("aesthetic", [])],
            }
        except (LcpError, OSError, ValueError) as e:
            if isinstance(e, LcpError):
                return _error_dict(e)
            return {"job_id": escape_html(job_id), "has_report": False}

    def approve(self, job_id: str, reviewer: str) -> dict:
        """Mirror `approve`: REVIEW_PENDING -> APPROVED. Reviewer MUST be in the
        config whitelist; non-REVIEW_PENDING source states are refused by the
        state machine (BLOCKED/NEEDS_HUMAN_REVIEW have NO path to APPROVED)."""
        try:
            c = self._ctx()
            # Load the persisted draft and pass it so signoff re-verifies the
            # frozen body hash — a draft tampered after freeze must NOT approve.
            draft = pl.load_draft(c.store, job_id)
            rec = signoff.approve(
                job_id, reviewer,
                config=c.config, store=c.store, audit=c.audit, ts=_now(),
                draft=draft,
            )
            return {
                "job_id": escape_html(job_id),
                "state": rec.new_state.value,
                "reviewer_stated": escape_html(rec.reviewer_stated),
                "observed_os_user": escape_html(rec.observed_os_user),
                "body_sha256": rec.body_sha256,
                "disclaimer": rec.disclaimer,
            }
        except LcpError as e:
            return _error_dict(e)

    def reject(self, job_id: str, reviewer: str, reason: str) -> dict:
        """Mirror `reject`: REVIEW_PENDING -> REJECTED (terminal)."""
        try:
            c = self._ctx()
            rec = signoff.reject(
                job_id, reviewer, reason,
                config=c.config, store=c.store, audit=c.audit, ts=_now(),
            )
            return {
                "job_id": escape_html(job_id),
                "state": rec.new_state.value,
                "reviewer_stated": escape_html(rec.reviewer_stated),
            }
        except LcpError as e:
            return _error_dict(e)

    def resolve(
        self,
        job_id: str,
        reviewer: str,
        relint: bool = False,
        reason: str | None = None,
    ) -> dict:
        """Mirror `resolve`: drive NEEDS_HUMAN_REVIEW -> PROCESSED.

        Grounding hold + relint=True: lint re-runs; a clean lint promotes.
        Risk/dedup hold (or grounding without relint): explicit reason required
        (a recorded human override). Reviewer MUST be whitelisted."""
        try:
            c = self._ctx()
            rec = signoff.resolve(
                job_id, reviewer,
                config=c.config, store=c.store, audit=c.audit, ts=_now(),
                relint=bool(relint), reason=reason,
            )
            return {
                "job_id": escape_html(job_id),
                "state": rec.new_state.value,
                "reviewer_stated": escape_html(rec.reviewer_stated),
            }
        except LcpError as e:
            return _error_dict(e)

    def backfill(self, job_id: str, reviewer: str, url: str, attested: bool) -> dict:
        """Mirror `backfill`: APPROVED -> PUBLISHED_RECORDED ONLY with a
        whitelisted reviewer, a non-empty URL AND the attestation tick. Without
        the tick the job stays APPROVED (the machine never publishes — R26/R37).
        The URL is never resolved."""
        try:
            c = self._ctx()
            new_state = signoff.backfill_published_url(
                job_id, url,
                config=c.config, store=c.store, audit=c.audit, ts=_now(),
                attested=bool(attested), reviewer=reviewer,
            )
            return {
                "job_id": escape_html(job_id),
                "state": new_state.value,
                "attested": bool(attested),
            }
        except LcpError as e:
            return _error_dict(e)

    def supersede(self, job_id: str, new_job_id: str | None = None) -> dict:
        """Mirror `supersede`: REVIEW_PENDING/APPROVED/NEEDS_REVISION ->
        SUPERSEDED, voiding the old sign-off and back-linking the new job."""
        try:
            c = self._ctx()
            new_state = signoff.supersede(
                job_id, store=c.store, audit=c.audit, ts=_now(), new_job_id=new_job_id,
            )
            return {
                "job_id": escape_html(job_id),
                "state": new_state.value,
                "new_job_id": escape_html(new_job_id) if new_job_id else None,
            }
        except LcpError as e:
            return _error_dict(e)

    # --- Worklist + home counts (G7) -----------------------------------------

    def list_jobs(self, state: str | None = None) -> dict:
        """Mirror `list`: the pull-style worklist, optionally filtered by state
        (alias or enum value). review_reason is escaped for display."""
        try:
            c = self._ctx()
            records = pl.list_jobs(c.store, state)
            rows = [
                {
                    "job_id": escape_html(r.job_id),
                    "state": r.state.value,
                    "review_reason": (
                        escape_html(r.review_reason.value) if r.review_reason else None
                    ),
                    "updated_at": escape_html(r.updated_at),
                }
                for r in records
            ]
            return {"jobs": rows, "count": len(rows)}
        except LcpError as e:
            return _error_dict(e)

    def summary(self) -> dict:
        """Mirror `list --summary`: home counts-by-state (G7)."""
        try:
            c = self._ctx()
            return {"summary": pl.batch_summary(c.store)}
        except LcpError as e:
            return _error_dict(e)

    # --- Dashboard: accumulated metrics (read-only aggregation) ---------------

    def dashboard_stats(self) -> dict:
        """Accumulated operational metrics for the dashboard view.

        Combines the PII-free jobs state counts (batch_summary) with an
        aggregation over audit.jsonl (per-gate intercept rates, review-reason
        counts, gate-to-gate intervals, daily throughput). Every value is a
        number or an enum/code/date string from OUR OWN vocabulary, so no
        escaping is needed; ``has_jobs`` lets the GUI render an onboarding empty
        state instead of a wall of zeros on first run.

        ``gate_intervals`` seconds INCLUDE operator wait time (ts is action time,
        not compute time) — the GUI labels them accordingly and they are NOT an
        optimization hint."""
        try:
            c = self._ctx()
            summary = pl.batch_summary(c.store)
            audit = aggregate_audit(c.audit.iter_events())
            gates = [
                {
                    "gate": g.gate,
                    "reached": g.reached,
                    "intercepted": g.intercepted,
                    "rate": g.rate,
                }
                for g in audit.gates
            ]
            return {
                "has_jobs": summary.get("total", 0) > 0,
                "summary": summary,
                "gates": gates,
                "review_reasons": audit.review_reasons,
                "gate_intervals": summarize_gaps(audit.gate_gaps),
                "daily_jobs": audit.daily_jobs,
            }
        except LcpError as e:
            return _error_dict(e)
        except Exception:  # noqa: BLE001 - bridge boundary, never leak a stack
            # dashboard_stats reads audit.jsonl, which a background crawl/process
            # thread is concurrently appending to. A non-LcpError IO/decode error
            # (locked/corrupt file) must NOT cross the bridge as a raw exception
            # (stack/path leak); return the same bridge-safe shape as _run_bg.
            return {"error": "internal error", "exit_code": EXIT_INTERNAL}

    # --- Saved sources: input reuse (PII-exception table) ---------------------

    def saved_sources(self) -> dict:
        """List reusable saved sources. ``label`` is escaped; ``source_ref`` is
        returned INERT (escaped — for DISPLAY only, never an <a href>, never
        fetched). ``source_ref_raw`` carries the verbatim value so the GUI can
        pre-fill it into the create-job input — the GUI MUST assign it only to an
        input ``.value`` (never innerHTML); submitting then re-runs the same
        crawl validation (allow_domains/robots) as manual entry."""
        try:
            c = self._ctx()
            rows = [
                {
                    "id": escape_html(s.id),
                    "label": escape_html(s.label),
                    "source_ref": inert_link(s.source_ref),
                    "source_ref_raw": s.source_ref,
                    "created_at": escape_html(s.created_at),
                }
                for s in c.sources.list_sources()
            ]
            return {"sources": rows, "count": len(rows)}
        except LcpError as e:
            return _error_dict(e)

    def add_saved_source(self, label: str, source_ref: str) -> dict:
        """Persist a reusable source (input reuse). Stored verbatim so it can be
        re-submitted; returned escaped/inert. NEVER writes the plaintext to audit."""
        try:
            c = self._ctx()
            s = c.sources.add_source(
                label=label, source_ref=source_ref, created_at=_now()
            )
            return {
                "id": escape_html(s.id),
                "label": escape_html(s.label),
                "source_ref": inert_link(s.source_ref),
                "saved": True,
            }
        except LcpError as e:
            return _error_dict(e)

    def delete_saved_source(self, source_id: str) -> dict:
        """Erase one saved source by opaque id (best-effort; see pii-inventory)."""
        try:
            c = self._ctx()
            removed = c.sources.delete_source(source_id)
            return {"id": escape_html(source_id), "removed": removed}
        except LcpError as e:
            return _error_dict(e)

    # --- Config-driven UI inputs ---------------------------------------------

    def reviewers(self) -> dict:
        """The reviewer whitelist for the dropdown (config.publisher.reviewers).

        Operator identifiers (not subject PII); escaped for safe rendering."""
        try:
            c = self._ctx()
            return {"reviewers": [escape_html(r) for r in c.config.publisher.reviewers]}
        except LcpError as e:
            return _error_dict(e)

    def disclaimer(self) -> dict:
        """The VERBATIM attribution-not-authentication disclaimer (unescaped: it
        is our own fixed text, never attacker-shapeable)."""
        return {"disclaimer": signoff.DISCLAIMER}

    # --- LLM settings (base_url/model -> file; api_key -> keyring ONLY) -------

    def _settings_path(self) -> Path:
        """Where save_settings writes. The configured path if one was given, else
        ``config.yaml`` in the working directory (the gitignored convention)."""
        return Path(self._config_path) if self._config_path else Path("config.yaml")

    def get_settings(self) -> dict:
        """Non-secret LLM settings + whether an api_key is set. NEVER returns the
        key (only a boolean). All strings escaped for safe rendering.

        ``allow_domains`` is exposed READ-ONLY (escaped) so the GUI onboarding can
        show whether the crawler allowlist is configured (a non-empty list) —
        there is no write path here; the allowlist stays a config.yaml-only
        compliance decision."""
        try:
            c = self._ctx()
            llm = c.config.llm
            return {
                "base_url": escape_html(llm.base_url),
                "model": escape_html(llm.model),
                "allowed_hosts": [escape_html(h) for h in llm.allowed_hosts],
                "allow_domains": [escape_html(d) for d in c.config.crawler.allow_domains],
                "api_key_set": c.config.has_api_key(),
                "config_path": escape_html(str(self._settings_path())),
            }
        except LcpError as e:
            return _error_dict(e)

    def save_settings(
        self, base_url: str = "", model: str = "", api_key: str = ""
    ) -> dict:
        """Persist base_url + model to the config file (its host auto-added to
        allowed_hosts) and, when api_key is non-empty, store it in the OS keyring.

        The api_key is NEVER written to a file, returned across the bridge, or
        logged. An empty api_key leaves the existing key untouched.

        Ordering matters: the key (the failure-prone, secret-bearing step) is
        stored in the keyring FIRST; the config file is written only after that
        succeeds, so a keyring failure aborts before any file mutation."""
        try:
            from urllib.parse import urlsplit

            base_url = (base_url or "").strip()
            model = (model or "").strip()
            host = _config.validate_llm_base_url(base_url)  # raises on bad shape

            # 1. Secret first — if this fails, nothing is persisted to the file.
            key_saved = False
            if api_key and api_key.strip():
                username = self._ctx().config.llm.keyring_username
                _config.set_llm_api_key(api_key, username=username)
                key_saved = True

            # 2. Then the file. A loopback http endpoint also needs its host in
            # allow_http_hosts to be usable at call time (client R40 gate).
            is_http = urlsplit(base_url).scheme.lower() == "http"
            _config.update_llm_config_file(
                self._settings_path(),
                base_url=base_url,
                model=model,
                allowed_hosts_add=host,
                allow_http_hosts_add=host if is_http else None,
            )
            out = self.get_settings()
            if "error" in out:
                return out
            out["saved"] = True
            out["key_saved"] = key_saved
            return out
        except LcpError as e:
            return _error_dict(e)


def _input_error(msg: str) -> LcpError:
    """Build an InputValidationError without importing it at the call site."""
    from .core.errors import InputValidationError

    return InputValidationError(msg)


def launch(config_path: str | None = None):  # pragma: no cover - desktop only
    """Open the desktop window. LAZY ``import webview`` — this is the ONLY place
    pywebview is imported, so the module stays importable (and Api stays testable)
    headless. NOT called in tests.

    Serves web/ via pywebview's built-in HTTP server bound to 127.0.0.1 ONLY
    (loopback; off-host unreachable). We point the window at the served
    index.html (NOT inline html=, which cannot load external app.js/cover.jpg and
    clashes with the no-inline CSP)."""
    import webview  # lazy: never imported at module top-level

    # Resolve a concrete config path so the Settings panel reads and writes the
    # SAME file the rest of the GUI loads (config.yaml in cwd by default; it is
    # gitignored). A missing file is tolerated — the panel creates it.
    config_path = config_path or "config.yaml"
    api = Api(config_path=config_path)
    webview.create_window(
        "Local Content Processor",
        url=str(WEB_DIR / "index.html"),
        js_api=api,
    )
    # http_server=True uses pywebview's built-in server, which binds to
    # SERVER_HOST (loopback) by default, so the window's assets are never
    # reachable off-host. There is no host= kwarg (passing one raises).
    # debug=True enables the WKWebView Web Inspector (right-click > Inspect
    # Element) so a silent JS failure is diagnosable. Loopback-only http_server
    # is unchanged; this only affects local devtools availability.
    webview.start(http_server=True, ssl=False, debug=True)
