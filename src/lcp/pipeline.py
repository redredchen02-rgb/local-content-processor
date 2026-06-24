"""Pipeline orchestration: inject adapters, run stages, manage state (Unit 8).

This is the imperative seam the plan's High-Level Design calls for: pipeline.py
injects the I/O adapters, calls the pure core, and drives the JobState machine
through JobStore + the shared persist seam. CLI and GUI shells stay thin by
calling these functions; ALL business judgement lives in core/adapters.

WHAT lives here:
  * :class:`Pipeline` — holds the injected adapters (store, audit, crawler,
    llm client) + config, and runs the stages.
  * :meth:`Pipeline.run_until` — the `run --until draft|review` flow: crawl/
    ingest (Stage 1) -> process (Stage 2) -> optionally build the review packet
    (Stage 4). Returns a :class:`RunResult` describing where the job came to
    rest (it may stop early at any gate: BLOCKED / DUPLICATE / NEEDS_*).
  * :meth:`Pipeline.process` — Stage 2: risk gate, media validation +
    normalization (images -> 800px, 1300x640 cover, video spec/black checks),
    dedup gate, constrained-rewrite assemble (dry_run aware), lint + grounding.
  * :func:`batch_summary` — counts-by-state for cron/batch (flow G5).
  * :func:`list_jobs` — pull-style worklist filtered by state (flow G5/G7).

dry_run (R32): threaded straight through to the LlmClient (constructed with
dry_run=True) and the assembler — in dry-run the LLM is NOT called and no
external system is mutated; the resulting Draft is marked NOT_EXECUTED. The
deterministic local stages (crawl/ingest already done, media, gates) still run,
but the process result is flagged ``dry_run=True`` so the shell can label it."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.container import Adapters as Adapters
from .adapters.crawler.base import (
    STATUS_CRAWL_FAILED,
    STATUS_CRAWLED,
    STATUS_CRAWLED_WARN,
    STATUS_NEEDS_REVISION,
    CrawlerProtocol,
    RawJobBundle,
    SourceSpec,
)
from .adapters.llm.assembler import assemble
from .adapters.llm.client import LlmClient
from .adapters.processor.draft_linter import build_lint_config, run_draft_lint_gate
from .adapters.publisher.review_packet import ReviewPacket, build_review_packet
from .adapters.storage.audit_log import EVENT_INTERRUPTED_DETECTED, AuditLog
from .adapters.storage.gossip_ingest import DEFAULT_MAX_ITEMS, IngestReport, ingest_items
from .adapters.processor._persist import persist_gate_state
from .adapters.storage.job_store import JobRecord, JobStore
from .core.config import Config
from .core.draft import Draft, DraftStatus
from .core.errors import DependencyError, ExternalServiceError, InputValidationError, LcpError
from .core.rules.risk_rules import RiskInput
from .core.state import TERMINAL_STATES, TRANSIENT_STATES, JobState

logger = logging.getLogger(__name__)

# Targets for run_until.
TARGET_DRAFT = "draft"
TARGET_REVIEW = "review"

# Persisted resting states a job can legally be in WHILE mid-Stage-2 (the
# .processing marker stands in for the transient PROCESSING). A marker found on
# one of these is a crash-interruption to surface to the operator (U7). Markers
# on any other (e.g. terminal) state are stale leftovers and are only cleared.
RECONCILABLE_STATES: frozenset[JobState] = frozenset(
    {JobState.CRAWLED, JobState.CRAWLED_WARN, JobState.PROCESS_FAILED, JobState.NEEDS_REVISION}
)

# States where a stale .processing marker is a crash-between-COMMIT-and-clear
# leftover that reconciliation only CLEARS (never reopens): the truly-terminal
# states PLUS BLOCKED/DUPLICATE. The latter two are gate resting states that U8
# moved out of TERMINAL_STATES (they gained an operator-only recovery edge), so
# they must be named explicitly here or their crash-leftover markers would leak.
_MARKER_ONLY_CLEAR_STATES: frozenset[JobState] = TERMINAL_STATES | frozenset(
    {JobState.BLOCKED, JobState.DUPLICATE}
)

# Crash-attempt cap before a deterministically-crashing job is flagged exhausted
# (surfaced to a human) instead of being treated as routinely re-processable.
DEFAULT_MAX_INTERRUPT_ATTEMPTS = 3


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` currently exists (POSIX target).

    reconcile() uses this to tell a LIVE .processing owner (a running process is
    mid-Stage-2 on the job) from a DEAD one (a hard crash left the marker behind).
    Signal 0 performs the existence/permission check without delivering a signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # no such process -> dead
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


# Map a crawl status string onto the persisted JobState after Stage 1.
_CRAWL_STATUS_TO_STATE: dict[str, JobState] = {
    STATUS_CRAWLED: JobState.CRAWLED,
    STATUS_CRAWLED_WARN: JobState.CRAWLED_WARN,
    STATUS_CRAWL_FAILED: JobState.CRAWL_FAILED,
    # needs_revision at crawl time means content incomplete; the job parks at
    # CRAWLED_WARN so the operator can decide (we never silently drop it).
    STATUS_NEEDS_REVISION: JobState.CRAWLED_WARN,
}


@dataclass(frozen=True)
class Stage1Result:
    """Outcome of Stage 1: the persisted record + the raw crawl status string.

    The status is carried alongside the record so a shell can report it without
    re-deriving the status->state mapping (which lives only in stage1)."""

    record: JobRecord
    crawl_status: str


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of Stage 2 for one job: the draft + where the job came to rest."""

    job_id: str
    draft: Draft | None
    final_state: JobState
    dry_run: bool
    # "risk" | "dedup" | "assemble" | "lint" | "error" | None (passed)
    stopped_at: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunResult:
    """Outcome of run_until: terminal-ish state + optional draft/packet."""

    job_id: str
    final_state: JobState
    target: str
    dry_run: bool
    draft: Draft | None = None
    packet: ReviewPacket | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InterruptedJob:
    """A job a crash left mid-Stage-2: a resting state + a stale .processing marker.

    Surfaced by :meth:`Pipeline.reconcile` for explicit operator re-process (we do
    NOT auto-transition — a hard crash is not a transient error). ``attempts`` is the
    crash-attempt count carried in the per-job-dir counter; ``exhausted`` is True once
    it exceeds the cap, the signal that a DETERMINISTIC crash needs a human (stop
    re-trying) rather than another retry->crash->retry loop."""

    job_id: str
    state: JobState
    attempts: int
    exhausted: bool


# Persisted-draft I/O lives in the storage layer (adapters/storage/draft_store).
# Re-exported here so the shells keep using pl.load_draft / pl.save_draft and the
# publisher imports them from storage (not upward from this orchestrator). The
# `X as X` form is an EXPLICIT re-export (required under no_implicit_reexport once
# this module is strict-checked).
from .adapters.storage.draft_store import (  # noqa: E402
    _DRAFT_NAME as _DRAFT_NAME,
)
from .adapters.storage.draft_store import (
    _draft_path as _draft_path,
)
from .adapters.storage.draft_store import (
    _read_source_text as _read_source_text,
)
from .adapters.storage.draft_store import (
    load_draft as load_draft,
)
from .adapters.storage.draft_store import (
    save_draft as save_draft,
)


class Pipeline:
    """Orchestrates the stages with injected adapters (functional core / shell).

    Accepts either a typed ``Adapters`` container (preferred) or individual
    adapter parameters (backward-compatible). The ``dry_run`` coercion/refusal
    (force-on or refuse) runs here at construction time — the ``Adapters``
    container is a data holder, not a policy enforcer."""

    def __init__(
        self,
        config: Config,
        store_or_adapters: JobStore | None = None,
        audit: AuditLog | None = None,
        *,
        dry_run: bool = False,
        crawler: CrawlerProtocol | None = None,
        llm_client: LlmClient | None = None,
        adapters: Adapters | None = None,
    ) -> None:
        _llm_client: LlmClient | None
        # Unpack from container or legacy positional parameters.
        if adapters is not None:
            self.config = config
            self.store = adapters.store
            self.audit = adapters.audit
            self.crawler = adapters.crawler
            _llm_client = adapters.llm_client
        else:
            # Legacy call site: Pipeline(config, store, audit, ...)
            if store_or_adapters is None or audit is None:
                raise InputValidationError(
                    "Pipeline requires either an Adapters container or "
                    "(config, store, audit) positional arguments"
                )
            self.config = config
            self.store = store_or_adapters
            self.audit = audit
            self.crawler = crawler
            _llm_client = llm_client

        self.dry_run = dry_run

        # dry_run is threaded into the client: in dry-run it never calls the API.
        # The R40 escape hatch (private-CA bundle / explicit http hosts) is
        # config-driven, so it is wired from config here, not just programmatic.
        if _llm_client is None:
            self.llm_client = LlmClient(
                config,
                dry_run=dry_run,
                ca_bundle=config.llm.ca_bundle,
                allow_http_hosts=config.llm.allow_http_hosts,
            )
        else:
            # An injected client must HONOUR dry_run — otherwise Pipeline(
            # dry_run=True, llm_client=<live>) would silently hit the API. If the
            # client exposes the dry-run flag we force it on; if it cannot be
            # forced and is not already dry, refuse (fail loud, never call live).
            if dry_run:
                if hasattr(_llm_client, "_dry_run"):
                    _llm_client._dry_run = True
                elif not getattr(_llm_client, "dry_run", False):
                    raise InputValidationError(
                        "dry_run=True but the injected llm_client cannot be put "
                        "in dry mode; refusing (it could call the live API)"
                    )
            self.llm_client = _llm_client

    # --- Stage 1: crawl / ingest --------------------------------------------

    def stage1(self, spec: SourceSpec, *, ts: str) -> Stage1Result:
        """Run Stage 1 (crawl or ingest) via the injected crawler, persist the
        derived state, and return the record + the raw crawl status.

        The crawler is injected (Scrapy / local-ingest / a fake) — the contract
        is the same RawJobBundle either way (proves the seam is real). This is the
        SINGLE owner of the Stage-1 sequence (create -> crawl -> map status ->
        persist (state + hashes) atomically); both shells call it, so the mapping
        and the never-park-at-NEW default live in exactly one place. The raw
        ``crawl_status`` is returned alongside the record so a shell can report it
        without re-deriving the mapping.

        Re-crawl semantics: a fresh crawl is legal for a brand-new job (no record
        yet), one created-but-not-yet-crawled (state NEW), or a CRAWL_FAILED job
        (bug_007). A CRAWL_FAILED crawl wrote NO bundle, so re-crawling clobbers
        nothing, and the state machine's CRAWL_FAILED -> NEW retry edge + the GUI
        重新抓取 affordance both expect an in-place retry: we reset it to NEW so
        persist_crawl_result's NEW -> outcome edge is legal. Re-crawling an
        ALREADY-crawled job (CRAWLED / CRAWLED_WARN / a processed state) WOULD
        clobber a real bundle and has no retry edge, so we refuse EARLY with an
        actionable error — before spawning the crawler / writing any bytes."""
        if self.crawler is None:
            raise InputValidationError("no crawler injected for Stage 1")
        existing = self.store.get_job(spec.job_id)
        if existing is None:
            self.store.create_job(spec.job_id, created_at=ts)
        elif existing.state is JobState.CRAWL_FAILED:
            # A failed crawl left no bundle to clobber; retry in place by resetting
            # to NEW (CRAWL_FAILED -> NEW is the state machine's retry edge). Without
            # this, the documented GUI 重新抓取 path dead-ended — supersede refuses
            # CRAWL_FAILED too, so the only recovery was delete + a fresh id.
            self.store.set_state(spec.job_id, JobState.NEW, updated_at=ts)
        elif existing.state is not JobState.NEW:
            # An existing already-crawled job: refuse before any crawl/mutation.
            # Mirrors ingest.py's create_only clobber guard (same intent — never
            # overwrite an existing bundle in place). The recovery is to delete the
            # job first, then crawl into a fresh id.
            raise InputValidationError(
                f"job {spec.job_id} already exists at {existing.state.value}; "
                "re-crawl is not supported (delete it first, or use a new --job-id)"
            )
        bundle: RawJobBundle = self.crawler.crawl(spec)
        target = _CRAWL_STATUS_TO_STATE.get(bundle.job_status, JobState.CRAWL_FAILED)
        # Persist the state transition AND the source hashes in ONE transaction so
        # a crash can never leave (state, hashes) torn (NEW-with-hashes or
        # CRAWLED-without-hashes).
        record = self.store.persist_crawl_result(
            spec.job_id,
            target,
            updated_at=ts,
            source_html_sha256=bundle.manifest.hashes.source_html_sha256,
            source_text_sha256=bundle.manifest.hashes.source_text_sha256,
        )
        return Stage1Result(record=record, crawl_status=bundle.job_status)

    # --- Stage 2: process ----------------------------------------------------

    def process(
        self,
        job_id: str,
        *,
        ts: str,
        title: str = "",
        risk_input: RiskInput | None = None,
        site_index_path: str | Path | None = None,
        has_videos: bool = False,
        watermark: bool | None = None,
        template: str | None = None,
        ai_copy: bool = False,
    ) -> ProcessResult:
        """Stage 2: risk gate -> media validation -> dedup gate -> assemble
        (dry_run aware) -> lint + grounding. Stops at the FIRST gate that parks
        the job.

        The job must rest at a legal PROCESSING-predecessor (CRAWLED /
        CRAWLED_WARN / PROCESS_FAILED — the last for a retry). A ``.processing``
        marker is set at entry and cleared in `finally` (crash detection); each
        gate uses the shared persist seam (PROCESSING -> resting state) and writes
        a PII-free audit event. On full pass the job lands PROCESSED.

        A truncated/empty draft from assemble (DraftStatus.NEEDS_REVISION)
        short-circuits to NEEDS_REVISION BEFORE grounding/lint, carrying the
        assembler's reason. An ExternalServiceError from assemble/gates (LLM 5xx /
        timeout) is mapped to PROCESS_FAILED (retriable) instead of leaving the
        job at CRAWLED.

        dry_run: the LLM client was built with dry_run=True, so `assemble`
        returns a NOT_EXECUTED stub (no API call, no tokens, no external
        mutation). The deterministic gates still run; the result is flagged."""
        record = self.store.get_job(job_id)
        if record is None:
            raise InputValidationError(f"unknown job: {job_id}")
        # PROCESS_FAILED is a legal entry too: a failed (e.g. LLM 5xx) run is
        # retriable (PROCESS_FAILED -> PROCESSING -> ...). NEEDS_REVISION is a
        # legal entry as well (U8): the operator re-runs a revised job in place
        # (NEEDS_REVISION -> PROCESSING -> target — the persist seam validates
        # that canonical edge; the edge is also wired in web/lex.js). Without
        # this the live NEEDS_REVISION -> PROCESSING edge was unreachable.
        if record.state not in (
            JobState.CRAWLED,
            JobState.CRAWLED_WARN,
            JobState.PROCESS_FAILED,
            JobState.NEEDS_REVISION,
        ):
            raise InputValidationError(
                f"process requires CRAWLED/CRAWLED_WARN/PROCESS_FAILED/"
                f"NEEDS_REVISION; {job_id} is {record.state.value}"
            )

        # A .processing marker ALREADY present at entry means a PRIOR process() for
        # this job died hard before its `finally` could clear it (a Python-level
        # error still runs finally; only a true crash skips it). Re-processing now is
        # therefore a crash RETRY: bump the loop-guard counter and record ONE
        # INTERRUPTED_DETECTED audit event HERE, on the actual retry — never on a
        # passive worklist view (reconcile() is a pure read; see bug_001). A
        # deterministically-crashing job thus surfaces to a human after N real
        # retries, not after N glances at the worklist.
        _was_crash_entry = False
        if self.store.is_processing(job_id):
            _was_crash_entry = True
            attempts = self.store.bump_interrupt_count(job_id)
            self.audit.append(
                ts=ts,
                stage="process",
                event=EVENT_INTERRUPTED_DETECTED,
                job_id=job_id,
                actor="system",
                extra={"attempts": attempts, "state": record.state.value},
            )

        # Mark the job mid-Stage-2 (transient PROCESSING) so a crash is
        # detectable and an LLM 5xx can be mapped to a retriable PROCESS_FAILED.
        # The marker is cleared in `finally`; the gates' persist_gate_state also
        # clears it when they park the job (clear is idempotent).
        self.store.mark_processing(job_id)
        _raised_service_error = False
        try:
            return self._process_inner(
                job_id,
                record=record,
                ts=ts,
                title=title,
                risk_input=risk_input,
                site_index_path=site_index_path,
                has_videos=has_videos,
                watermark=watermark,
                template=template,
                ai_copy=ai_copy,
            )
        except (ExternalServiceError, DependencyError):
            # An LLM/network failure or missing dependency (e.g. API key not
            # configured) mid-process must not leave the job at CRAWLED.
            # Persist PROCESS_FAILED (retriable: PROCESS_FAILED -> PROCESSING)
            # so the operator can see the failure and re-run after fixing config.
            # persist_gate_state re-marks + clears its own marker.
            _raised_service_error = True
            persist_gate_state(
                self.store,
                job_id,
                JobState.PROCESS_FAILED,
                updated_at=ts,
                error_code="llm_external_error",
            )
            return ProcessResult(
                job_id=job_id,
                draft=None,
                final_state=JobState.PROCESS_FAILED,
                dry_run=self.dry_run,
                stopped_at="error",
                notes=["LLM/external service error; PROCESS_FAILED (retriable)"],
            )
        finally:
            # Any Python-level completion (return OR raise) means this run did NOT
            # die hard, so the crash loop-guard clears — UNLESS this was a crash
            # recovery that ended in a service error: in that case, keep the counter
            # so that crash→ExternalServiceError→crash sequences reach max_attempts
            # and surface the deterministically-failing job to a human.
            self.store.clear_processing(job_id)
            if not (_was_crash_entry and _raised_service_error):
                self.store.clear_interrupt_count(job_id)

    def _process_inner(
        self,
        job_id: str,
        *,
        record: JobRecord,
        ts: str,
        title: str,
        risk_input: RiskInput | None,
        site_index_path: str | Path | None,
        has_videos: bool,
        watermark: bool | None = None,
        template: str | None = None,
        ai_copy: bool = False,
    ) -> ProcessResult:
        """Stage-2 gate sequence (called inside the .processing marker scope).

        Raises ExternalServiceError up to `process` (which maps it to
        PROCESS_FAILED); every other resting state is returned as a
        ProcessResult."""
        source_text = _read_source_text(self.store, job_id)
        notes: list[str] = []

        # --- park-or-pass gate chain (risk -> media -> dedup) ---
        # Declarative registry: order is data, fail-closed preserved. Each gate
        # returns None (pass) or a JobState (park). The runner derives
        # ``stopped_at`` from the gate name.
        from .adapters.processor.gate_registry import (
            PARK_GATES,
            GateContext,
            run_gate_chain,
        )

        ctx = GateContext(
            job_id=job_id,
            store=self.store,
            audit=self.audit,
            ts=ts,
            title=title,
            source_text=source_text,
            risk_input=risk_input,
            site_index_path=site_index_path,
            watermark_enabled=watermark,
            media_config=self.config.media,
            watermark_config=self.config.watermark,
        )
        parked_state, stopped_at = run_gate_chain(PARK_GATES, ctx)
        if parked_state is not None:
            return ProcessResult(
                job_id=job_id,
                draft=None,
                final_state=parked_state,
                dry_run=self.dry_run,
                stopped_at=stopped_at,
                notes=notes,
            )
        # Stash media report for lint (has_images).
        media_out_report = ctx.reports.get("media", {})

        # --- constrained rewrite (dry_run aware: NO API call in dry-run) ---
        # An ExternalServiceError here propagates to `process` -> PROCESS_FAILED.
        # A 栏目 template (process-time input) is resolved + linted here and
        # rendered into the DEVELOPER slot (never SYSTEM); unknown category -> no
        # template (assemble runs as before).
        from .adapters.llm import templates as tmpl

        resolved_template = tmpl.get_template(self.config, template)
        template_values = (
            {"category": template or "", "title": title or ""} if resolved_template else None
        )
        draft = assemble(
            source_text,
            self.llm_client,
            title=title or None,
            category=template,
            template=resolved_template,
            template_values=template_values,
        )

        # Optional AI structural copy (captions/FAQ/subheads), process-time opt-in.
        # Dry-run / truncated -> skipped (no silent partial); the pieces are
        # grounded + freeze-bound downstream like the body.
        if ai_copy and draft.status is DraftStatus.DRAFTED:
            from .adapters.llm.copywriter import (
                apply_copy_to_draft,
                generate_structural_copy,
            )

            copy = generate_structural_copy(source_text, self.llm_client)
            if copy.executed and not copy.needs_revision:
                draft = apply_copy_to_draft(draft, copy)
                notes.append("ai-copy: structural pieces generated (needs review)")

        # Persist the assembled draft so review-packet freezes THIS exact draft
        # (no re-assembly). In dry-run the draft is a NOT_EXECUTED stub.
        save_draft(self.store, job_id, draft)
        if self.dry_run:
            notes.append("dry-run: LLM not executed; no external mutation")

        # --- assemble verdict: a truncated/empty draft (NEEDS_REVISION) must
        # short-circuit BEFORE grounding/lint, carrying the assembler's reason
        # (otherwise the truncation review_reason is silently lost). ---
        if draft.status is DraftStatus.NEEDS_REVISION:

            persist_gate_state(self.store, job_id, JobState.NEEDS_REVISION, updated_at=ts)
            if draft.review_reason:
                notes.append(f"assemble: {draft.review_reason}")
            return ProcessResult(
                job_id=job_id,
                draft=draft,
                final_state=JobState.NEEDS_REVISION,
                dry_run=self.dry_run,
                stopped_at="assemble",
                notes=notes,
            )

        # --- lint + grounding gate ---
        # Derive has_images from the actual media gate result (D9): image_sections
        # is required IFF the bundle has images. has_videos stays the caller hint.
        has_images = bool(media_out_report.get("image_count", 0))
        lint_config = build_lint_config(self.config.content, self.config.categories)
        lint_out = run_draft_lint_gate(
            job_id=job_id,
            draft=draft,
            source_text=source_text,
            lint_config=lint_config,
            store=self.store,
            audit=self.audit,
            ts=ts,
            has_videos=has_videos,
            has_images=has_images,
        )
        if lint_out.job_state is not None:
            # Surface WHICH sections/claims failed so the operator (CLI/GUI) can
            # act without re-deriving them. PII-free: lint errors are canonical
            # section labels + counts; grounding only a boolean reason here.
            park_notes = list(notes)
            if lint_out.lint is not None:
                park_notes.extend(f"lint: {e}" for e in lint_out.lint.errors)
            if lint_out.grounding is not None and lint_out.grounding.needs_human_review:
                park_notes.append("grounding: ungrounded claim(s) need human review")
            return ProcessResult(
                job_id=job_id,
                draft=draft,
                final_state=lint_out.job_state,
                dry_run=self.dry_run,
                stopped_at="lint",
                notes=park_notes,
            )

        # --- all gates passed: PROCESSING -> PROCESSED ---

        persist_gate_state(self.store, job_id, JobState.PROCESSED, updated_at=ts)
        return ProcessResult(
            job_id=job_id,
            draft=draft,
            final_state=JobState.PROCESSED,
            dry_run=self.dry_run,
            stopped_at=None,
            notes=notes,
        )

    # --- Stage 4: review packet (the freeze + PROCESSED -> REVIEW_PENDING) ---

    def ingest_gossip(
        self,
        items: list[dict[str, object]],
        *,
        ts: str,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> IngestReport:
        """Create one NEW lcp job per GossipItem (persisting each source URL to
        the job bundle for the deferred crawl). Thin wrapper so the shells don't
        re-implement the ingest orchestration (plan 001, Unit 4)."""
        return ingest_items(items, self.store, ts=ts, max_items=max_items)

    def build_packet(
        self,
        job_id: str,
        draft: Draft,
        *,
        ts: str,
        source_urls: list[str] | None = None,
        processed_cover: str | None = None,
        actor: str = "human",
    ) -> ReviewPacket:
        """Build the sanitized review packet (freezes the draft). Thin wrapper so
        the shell does not import the adapter directly."""
        return build_review_packet(
            job_id=job_id,
            draft=draft,
            store=self.store,
            audit=self.audit,
            submitted_at=ts,
            source_urls=source_urls,
            processed_cover=processed_cover,
            actor=actor,
        )

    # --- run --until draft|review -------------------------------------------

    def run_until(
        self,
        spec: SourceSpec,
        *,
        target: str,
        ts: str,
        title: str = "",
        source_urls: list[str] | None = None,
        processed_cover: str | None = None,
        site_index_path: str | Path | None = None,
        has_videos: bool = False,
        ai_copy: bool = True,
        template: str | None = None,
        watermark: bool | None = None,
    ) -> RunResult:
        """Run the pipeline up to `target` ('draft' or 'review').

        - target='draft': Stage 1 (crawl/ingest) -> Stage 2 (process). Stops at
          PROCESSED, or earlier if a gate parks the job (BLOCKED / DUPLICATE /
          NEEDS_*).
        - target='review': as 'draft', then (only if PROCESSED) build the review
          packet -> REVIEW_PENDING.

        `ai_copy` defaults ON for the happy path (D2): the copywriter fills the
        required quick_facts/summary/faq (and image captions), without which a
        real draft cannot reach PROCESSED. A dry run skips the LLM regardless.

        dry_run is honoured throughout (LLM never called). Stops EARLY and
        returns the resting state if any gate parks the job — it never forces a
        job past a gate."""
        if target not in (TARGET_DRAFT, TARGET_REVIEW):
            raise InputValidationError(f"--until must be 'draft' or 'review' (got {target!r})")

        rec = self.stage1(spec, ts=ts).record
        if rec.state not in (JobState.CRAWLED, JobState.CRAWLED_WARN):
            return RunResult(
                job_id=spec.job_id,
                final_state=rec.state,
                target=target,
                dry_run=self.dry_run,
                notes=[f"stage 1 ended at {rec.state.value}"],
            )

        proc = self.process(
            spec.job_id,
            ts=ts,
            title=title,
            site_index_path=site_index_path,
            has_videos=has_videos,
            ai_copy=ai_copy,
            template=template,
            watermark=watermark,
        )
        if proc.final_state is not JobState.PROCESSED:
            notes = list(proc.notes)
            if proc.stopped_at:
                notes.append(f"stopped at gate: {proc.stopped_at}")
            return RunResult(
                job_id=spec.job_id,
                final_state=proc.final_state,
                target=target,
                dry_run=self.dry_run,
                draft=proc.draft,
                notes=notes,
            )

        if target == TARGET_DRAFT:
            return RunResult(
                job_id=spec.job_id,
                final_state=JobState.PROCESSED,
                target=target,
                dry_run=self.dry_run,
                draft=proc.draft,
                notes=proc.notes,
            )

        # target == review: build the packet (freeze -> REVIEW_PENDING).
        # Reaching here means proc.final_state is PROCESSED, which guarantees a
        # draft. Guard the invariant explicitly (fail closed; also narrows the
        # Optional for the type checker).
        if proc.draft is None:  # pragma: no cover - PROCESSED always carries a draft
            raise LcpError(f"internal: PROCESSED job {spec.job_id} has no draft")
        packet = self.build_packet(
            spec.job_id,
            proc.draft,
            ts=ts,
            source_urls=source_urls,
            processed_cover=processed_cover,
        )
        return RunResult(
            job_id=spec.job_id,
            final_state=JobState.REVIEW_PENDING,
            target=target,
            dry_run=self.dry_run,
            draft=proc.draft,
            packet=packet,
            notes=proc.notes,
        )

    # --- crash reconciliation: the .processing marker's consumer (U7) --------

    def reconcile(
        self, *, max_attempts: int = DEFAULT_MAX_INTERRUPT_ATTEMPTS
    ) -> list[InterruptedJob]:
        """Find jobs a crash left mid-Stage-2 and surface them for the operator.

        This is the marker's real consumer (it had none): something must read
        ``is_processing()`` at a worklist lifecycle boundary. Both shells call it
        from their worklist entry point (CLI ``list`` / GUI ``list_jobs``), so a
        stale ``.processing`` marker becomes visible the moment the operator looks
        at the worklist.

        **This is a PURE READ** (bug_001): it never bumps a counter and never writes
        an audit event. A worklist view is read-only — the crash loop-guard counter
        is bumped by ``process()`` on the actual retry, and reconcile only *reads*
        it. (Previously it bumped on every view, so four glances at the worklist
        flagged a job ``exhausted`` and spammed the hash-chained audit log.)

        It also distinguishes a LIVE marker from a stale one by the PID stamped into
        the marker: a marker owned by THIS process or any still-running process is
        in-flight work (e.g. a GUI background-thread crawl mid-Stage-2 has its job
        resting at CRAWLED with a live marker) and is skipped — NOT mis-flagged as a
        crash. Only a marker whose owner PID is dead (or unreadable/legacy) is a real
        hard-crash leftover.

        We **flag, never auto-transition**: a hard crash is NOT a transient
        ``ExternalServiceError``, so silently re-driving it risks a deterministic
        retry->crash->retry loop. The job keeps its resting state and marker; the
        operator re-processes deliberately. Once the (process-bumped) crash counter
        exceeds ``max_attempts`` the job is flagged ``exhausted``.

        A marker found on a TERMINAL job (a crash between COMMIT and
        ``clear_processing``) is only CLEARED — reconciliation never reopens a
        terminal job (that would be the content-laundering path the freeze model
        forbids)."""
        interrupted: list[InterruptedJob] = []
        for rec in self.store.list_all():
            if not self.store.is_processing(rec.job_id):
                continue
            owner = self.store.processing_owner_pid(rec.job_id)
            if owner is not None and (owner == os.getpid() or _pid_alive(owner)):
                # A live process owns this marker: it is in-flight work, not a
                # crash. Never flag (would be a false positive on a healthy job
                # being processed right now) and never clear (would yank a live
                # process's marker out from under it).
                continue
            if rec.state in RECONCILABLE_STATES:
                attempts = self.store.read_interrupt_count(rec.job_id)
                interrupted.append(
                    InterruptedJob(
                        job_id=rec.job_id,
                        state=rec.state,
                        attempts=attempts,
                        exhausted=attempts > max_attempts,
                    )
                )
            elif rec.state in _MARKER_ONLY_CLEAR_STATES:
                # Crash between COMMIT and clear_processing: the resting state is
                # already correct; just drop the stale marker (never reopen).
                # BLOCKED/DUPLICATE are included explicitly: U8 moved them OUT of
                # TERMINAL_STATES (they now carry an operator-only recovery edge),
                # but a stale marker on them is still a crash leftover to clear,
                # NOT a reopen — reconciliation must never auto-recover them.
                self.store.clear_processing(rec.job_id)
            # Any other state with a marker (e.g. a persisted PROCESSING is
            # impossible — it is transient) is left untouched: we neither flag nor
            # clear, since it is not a recognised crash-interruption shape.
        return interrupted

    def reconcile_one(
        self, job_id: str, *, max_attempts: int = DEFAULT_MAX_INTERRUPT_ATTEMPTS
    ) -> InterruptedJob | None:
        """Single-job variant of reconcile(): O(1) instead of O(n).

        Returns the InterruptedJob if the job has a stale .processing marker,
        or None if the job is not interrupted. Used by get_job() to avoid a
        full worklist scan just to check one job (get-job-reconcile-full-scan)."""
        rec = self.store.get_job(job_id)
        if rec is None or not self.store.is_processing(job_id):
            return None
        owner = self.store.processing_owner_pid(job_id)
        if owner is not None and (owner == os.getpid() or _pid_alive(owner)):
            return None
        if rec.state in RECONCILABLE_STATES:
            attempts = self.store.read_interrupt_count(job_id)
            return InterruptedJob(
                job_id=job_id,
                state=rec.state,
                attempts=attempts,
                exhausted=attempts > max_attempts,
            )
        if rec.state in _MARKER_ONLY_CLEAR_STATES:
            self.store.clear_processing(job_id)
        return None


# --- pull-style worklist + batch summary (flow G5/G7) -----------------------

# Convenient CLI aliases (--state pending|blocked|needs-review|duplicate|...).
STATE_ALIASES: dict[str, JobState] = {
    "pending": JobState.REVIEW_PENDING,
    "review-pending": JobState.REVIEW_PENDING,
    "blocked": JobState.BLOCKED,
    "duplicate": JobState.DUPLICATE,
    "needs-review": JobState.NEEDS_HUMAN_REVIEW,
    "needs-human-review": JobState.NEEDS_HUMAN_REVIEW,
    "needs-revision": JobState.NEEDS_REVISION,
    "approved": JobState.APPROVED,
    "processed": JobState.PROCESSED,
    "rejected": JobState.REJECTED,
    "published": JobState.PUBLISHED_RECORDED,
    "published-recorded": JobState.PUBLISHED_RECORDED,
    "superseded": JobState.SUPERSEDED,
    "new": JobState.NEW,
    "crawled": JobState.CRAWLED,
    "crawled-warn": JobState.CRAWLED_WARN,
    "crawl-failed": JobState.CRAWL_FAILED,
    "process-failed": JobState.PROCESS_FAILED,
}


def resolve_state(name: str) -> JobState:
    """Resolve a CLI state alias or raw enum value to a JobState."""
    key = name.strip().lower()
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    try:
        return JobState(key)
    except ValueError as e:
        raise InputValidationError(
            f"unknown state filter: {name!r} (try one of {sorted(STATE_ALIASES)!r})"
        ) from e


def list_jobs(store: JobStore, state: str | JobState | None = None) -> list[JobRecord]:
    """Pull-style worklist (flow G5/G7).

    With `state` -> jobs in that state (alias or enum). Without -> ALL persisted
    jobs (PROCESSING is never persisted, so it never appears — by design). Sorted
    by created_at then job_id for stable, paste-able output."""
    if state is not None:
        st = resolve_state(state) if isinstance(state, str) else state
        return store.list_by_state(st)
    # No filter: one connection (list_all) instead of one per JobState. PROCESSING
    # is never persisted, so transient states never appear.
    return store.list_all()


def batch_summary(store: JobStore) -> dict[str, int]:
    """Counts-by-state for cron/batch (flow G5).

    Returns {state_value: count} for every state that has at least one job, plus
    a synthetic 'total'. PROCESSING is transient and never counted (not
    persisted). This is the pull-style summary the operator reads after a batch
    run — there is no push notification in the MVP."""
    counts = store.counts_by_state()  # one GROUP BY query, not one per state
    summary: dict[str, int] = {}
    # Preserve the previous deterministic JobState-order output (the raw GROUP BY
    # order is unspecified); transient states never appear in `counts`.
    for st in JobState:
        if st in TRANSIENT_STATES:
            continue
        n = counts.get(st.value, 0)
        if n:
            summary[st.value] = n
    summary["total"] = sum(summary.values())
    return summary


def process_batch(
    pipeline: Pipeline,
    state: str | JobState,
    *,
    ts: str,
    title: str = "",
    ai_copy: bool = False,
    watermark: bool | None = None,
    template: str | None = None,
) -> list[ProcessResult]:
    """Process every job in a given state independently.

    Continues past parked/failed jobs (each job is independent). Returns a list
    of ``ProcessResult`` — one per job processed. Emits a PII-free
    ``BATCH_SUMMARY`` audit event with per-state counts."""
    from .adapters.storage.audit_log import EVENT_BATCH_SUMMARY

    st = resolve_state(state) if isinstance(state, str) else state
    jobs = list_jobs(pipeline.store, st)
    results: list[ProcessResult] = []
    for rec in jobs:
        try:
            res = pipeline.process(
                rec.job_id,
                ts=ts,
                title=title,
                ai_copy=ai_copy,
                watermark=watermark,
                template=template,
            )
        except Exception:  # noqa: BLE001 - batch boundary: isolate per-job failure
            # One job's failure (an unexpected crash, or a domain error such as an
            # illegal entry state for this batch's source state) must NOT abort the
            # remaining jobs. The job keeps its caller-owned .processing marker and
            # surfaces as interrupted via reconcile() later — never silently
            # auto-run. job_id is a non-PII operator id, safe to log.
            logger.warning("process_batch: job %s did not complete; continuing", rec.job_id)
            continue
        results.append(res)

    # Emit PII-free batch summary: {state_code: count} only.
    if results:
        counts: dict[str, int] = {}
        for r in results:
            key = r.final_state.value
            counts[key] = counts.get(key, 0) + 1
        pipeline.audit.append(
            ts=ts,
            stage="batch",
            event=EVENT_BATCH_SUMMARY,
            job_id="batch",
            actor="system",
            extra={"source_state": st.value, "processed": len(results), "outcomes": counts},
        )

    return results
