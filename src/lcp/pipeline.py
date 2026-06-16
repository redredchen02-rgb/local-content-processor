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
  * :meth:`Pipeline.process` — Stage 2: media validate/normalize, risk gate,
    dedup gate, constrained-rewrite assemble (dry_run aware), lint + grounding.
  * :func:`batch_summary` — counts-by-state for cron/batch (flow G5).
  * :func:`list_jobs` — pull-style worklist filtered by state (flow G5/G7).

dry_run (R32): threaded straight through to the LlmClient (constructed with
dry_run=True) and the assembler — in dry-run the LLM is NOT called and no
external system is mutated; the resulting Draft is marked NOT_EXECUTED. The
deterministic local stages (crawl/ingest already done, media, gates) still run,
but the process result is flagged ``dry_run=True`` so the shell can label it."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.crawler.base import (
    STATUS_CRAWL_FAILED,
    STATUS_CRAWLED,
    STATUS_CRAWLED_WARN,
    STATUS_NEEDS_REVISION,
    Crawler,
    RawJobBundle,
    SourceSpec,
)
from .adapters.llm.assembler import assemble
from .adapters.llm.client import LlmClient
from .adapters.processor import dedup_checker, risk_checker
from .adapters.processor.draft_linter import build_lint_config, run_draft_lint_gate
from .adapters.publisher.review_packet import ReviewPacket, build_review_packet
from .adapters.storage.audit_log import AuditLog
from .adapters.storage.job_store import JobRecord, JobStore
from .core.config import Config
from .core.draft import Draft, DraftStatus
from .core.errors import ExternalServiceError, InputValidationError
from .core.models import SourceType
from .core.rules.risk_rules import RiskInput
from .core.state import JobState, TRANSIENT_STATES

# Targets for run_until.
TARGET_DRAFT = "draft"
TARGET_REVIEW = "review"

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


def _read_source_text(store: JobStore, job_id: str) -> str:
    """Read the scraped/ingested body text from the raw bundle (source.txt).

    Local string read only — never parses or fetches a URL (R41)."""
    path = store.job_dir(job_id) / "raw" / "source.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


_DRAFT_NAME = "draft.json"


def _draft_path(store: JobStore, job_id: str) -> Path:
    return store.job_dir(job_id) / "processed" / _DRAFT_NAME


def save_draft(store: JobStore, job_id: str, draft: Draft) -> Path:
    """Persist the assembled draft to data/jobs/<id>/processed/draft.json (0600).

    This is what the review-packet command reads back to FREEZE — so the freeze
    binds the exact draft Stage 2 produced, not a re-assembled (and therefore
    non-deterministic) one. Plaintext 0600, best-effort deletion (R42).

    Atomic: temp in the same dir + fsync + os.replace, with chmod 0600 on the
    temp before the replace (so the committed draft is never world-readable and a
    crash mid-write never leaves a torn draft.json)."""
    path = _draft_path(store, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(draft.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def load_draft(store: JobStore, job_id: str) -> Draft | None:
    """Read back the persisted Stage-2 draft, or None if it was never produced."""
    path = _draft_path(store, job_id)
    if not path.exists():
        return None
    return Draft.model_validate_json(path.read_text(encoding="utf-8"))


class Pipeline:
    """Orchestrates the stages with injected adapters (functional core / shell)."""

    def __init__(
        self,
        config: Config,
        store: JobStore,
        audit: AuditLog,
        *,
        dry_run: bool = False,
        crawler: Crawler | None = None,
        llm_client: LlmClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.dry_run = dry_run
        self.crawler = crawler
        # dry_run is threaded into the client: in dry-run it never calls the API.
        # The R40 escape hatch (private-CA bundle / explicit http hosts) is
        # config-driven, so it is wired from config here, not just programmatic.
        if llm_client is None:
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
                if hasattr(llm_client, "_dry_run"):
                    llm_client._dry_run = True
                elif not getattr(llm_client, "dry_run", False):
                    raise InputValidationError(
                        "dry_run=True but the injected llm_client cannot be put "
                        "in dry mode; refusing (it could call the live API)"
                    )
            self.llm_client = llm_client

    # --- Stage 1: crawl / ingest --------------------------------------------

    def stage1(self, spec: SourceSpec, *, ts: str) -> JobRecord:
        """Run Stage 1 (crawl or ingest) via the injected crawler, persist the
        derived state, and return the job record.

        The crawler is injected (Scrapy / local-ingest / a fake) — the contract
        is the same RawJobBundle either way (proves the seam is real)."""
        if self.crawler is None:
            raise InputValidationError("no crawler injected for Stage 1")
        existing = self.store.get_job(spec.job_id)
        if existing is None:
            self.store.create_job(spec.job_id, created_at=ts)
        bundle: RawJobBundle = self.crawler.crawl(spec)
        target = _CRAWL_STATUS_TO_STATE.get(bundle.job_status, JobState.CRAWL_FAILED)
        self.store.set_hashes(
            spec.job_id,
            updated_at=ts,
            source_html_sha256=bundle.manifest.hashes.source_html_sha256,
            source_text_sha256=bundle.manifest.hashes.source_text_sha256,
        )
        return self.store.set_state(spec.job_id, target, updated_at=ts)

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
    ) -> ProcessResult:
        """Stage 2: risk gate -> dedup gate -> assemble (dry_run aware) -> lint +
        grounding. Stops at the FIRST gate that parks the job.

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
        # retriable (PROCESS_FAILED -> PROCESSING -> ...).
        if record.state not in (
            JobState.CRAWLED, JobState.CRAWLED_WARN, JobState.PROCESS_FAILED
        ):
            raise InputValidationError(
                f"process requires CRAWLED/CRAWLED_WARN/PROCESS_FAILED; {job_id} "
                f"is {record.state.value}"
            )

        # Mark the job mid-Stage-2 (transient PROCESSING) so a crash is
        # detectable and an LLM 5xx can be mapped to a retriable PROCESS_FAILED.
        # The marker is cleared in `finally`; the gates' persist_gate_state also
        # clears it when they park the job (clear is idempotent).
        self.store.mark_processing(job_id)
        try:
            return self._process_inner(
                job_id,
                record=record,
                ts=ts,
                title=title,
                risk_input=risk_input,
                site_index_path=site_index_path,
                has_videos=has_videos,
            )
        except ExternalServiceError:
            # An LLM/network failure mid-process must not leave the job at
            # CRAWLED. Persist PROCESS_FAILED (retriable: PROCESS_FAILED ->
            # PROCESSING) so a re-run picks it up. persist_gate_state re-marks +
            # clears its own marker.
            from .adapters.processor._persist import persist_gate_state

            persist_gate_state(
                self.store, job_id, JobState.PROCESS_FAILED, updated_at=ts,
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
            self.store.clear_processing(job_id)

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
    ) -> ProcessResult:
        """Stage-2 gate sequence (called inside the .processing marker scope).

        Raises ExternalServiceError up to `process` (which maps it to
        PROCESS_FAILED); every other resting state is returned as a
        ProcessResult."""
        source_text = _read_source_text(self.store, job_id)
        notes: list[str] = []

        # --- risk gate (fail-closed; redline -> BLOCKED terminal) ---
        ri = risk_input or RiskInput(title=title, body=source_text)
        risk_out = risk_checker.run_risk_gate(
            job_id=job_id,
            content=ri,
            store=self.store,
            audit=self.audit,
            ts=ts,
        )
        if risk_out.job_state is not None:
            return ProcessResult(
                job_id=job_id,
                draft=None,
                final_state=risk_out.job_state,
                dry_run=self.dry_run,
                stopped_at="risk",
                notes=notes,
            )

        # --- dedup gate (advisory; duplicate -> DUPLICATE, never auto-reject) ---
        dedup_out = dedup_checker.run_dedup_gate(
            job_id=job_id,
            title=title,
            body=source_text,
            store=self.store,
            audit=self.audit,
            ts=ts,
            site_index_path=site_index_path,
        )
        if dedup_out.job_state is not None:
            return ProcessResult(
                job_id=job_id,
                draft=None,
                final_state=dedup_out.job_state,
                dry_run=self.dry_run,
                stopped_at="dedup",
                notes=notes,
            )

        # --- constrained rewrite (dry_run aware: NO API call in dry-run) ---
        # An ExternalServiceError here propagates to `process` -> PROCESS_FAILED.
        draft = assemble(
            source_text,
            self.llm_client,
            title=title or None,
        )
        # Persist the assembled draft so review-packet freezes THIS exact draft
        # (no re-assembly). In dry-run the draft is a NOT_EXECUTED stub.
        save_draft(self.store, job_id, draft)
        if self.dry_run:
            notes.append("dry-run: LLM not executed; no external mutation")

        # --- assemble verdict: a truncated/empty draft (NEEDS_REVISION) must
        # short-circuit BEFORE grounding/lint, carrying the assembler's reason
        # (otherwise the truncation review_reason is silently lost). ---
        if draft.status is DraftStatus.NEEDS_REVISION:
            from .adapters.processor._persist import persist_gate_state

            persist_gate_state(
                self.store, job_id, JobState.NEEDS_REVISION, updated_at=ts
            )
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
        )
        if lint_out.job_state is not None:
            return ProcessResult(
                job_id=job_id,
                draft=draft,
                final_state=lint_out.job_state,
                dry_run=self.dry_run,
                stopped_at="lint",
                notes=notes,
            )

        # --- all gates passed: PROCESSING -> PROCESSED ---
        from .adapters.processor._persist import persist_gate_state

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
    ) -> RunResult:
        """Run the pipeline up to `target` ('draft' or 'review').

        - target='draft': Stage 1 (crawl/ingest) -> Stage 2 (process). Stops at
          PROCESSED, or earlier if a gate parks the job (BLOCKED / DUPLICATE /
          NEEDS_*).
        - target='review': as 'draft', then (only if PROCESSED) build the review
          packet -> REVIEW_PENDING.

        dry_run is honoured throughout (LLM never called). Stops EARLY and
        returns the resting state if any gate parks the job — it never forces a
        job past a gate."""
        if target not in (TARGET_DRAFT, TARGET_REVIEW):
            raise InputValidationError(
                f"--until must be 'draft' or 'review' (got {target!r})"
            )

        rec = self.stage1(spec, ts=ts)
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
            f"unknown state filter: {name!r} (try one of "
            f"{sorted(STATE_ALIASES)!r})"
        ) from e


def list_jobs(
    store: JobStore, state: str | JobState | None = None
) -> list[JobRecord]:
    """Pull-style worklist (flow G5/G7).

    With `state` -> jobs in that state (alias or enum). Without -> ALL persisted
    jobs (PROCESSING is never persisted, so it never appears — by design). Sorted
    by created_at then job_id for stable, paste-able output."""
    if state is not None:
        st = resolve_state(state) if isinstance(state, str) else state
        return store.list_by_state(st)
    out: list[JobRecord] = []
    for st in JobState:
        if st in TRANSIENT_STATES:
            continue
        out.extend(store.list_by_state(st))
    out.sort(key=lambda r: (r.created_at, r.job_id))
    return out


def batch_summary(store: JobStore) -> dict[str, int]:
    """Counts-by-state for cron/batch (flow G5).

    Returns {state_value: count} for every state that has at least one job, plus
    a synthetic 'total'. PROCESSING is transient and never counted (not
    persisted). This is the pull-style summary the operator reads after a batch
    run — there is no push notification in the MVP."""
    summary: dict[str, int] = {}
    total = 0
    for st in JobState:
        if st in TRANSIENT_STATES:
            continue
        n = len(store.list_by_state(st))
        if n:
            summary[st.value] = n
            total += n
    summary["total"] = total
    return summary
