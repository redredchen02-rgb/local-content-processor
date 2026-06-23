"""CLI shell (imperative). Thin: parse args -> call pipeline/core -> format.

Business logic lives in core/adapters/pipeline; this shell only:
  * parses args + global flags (--config/--dry-run/--json/--output-dir/--quiet),
  * builds the injected adapters (config, JobStore, AuditLog, crawler, llm),
  * calls the pipeline / publisher functions,
  * formats output (plain or --json),
  * reads error.exit_code in main() to decide the process exit status (R30/R31).

Every operator action has a CLI command so the GUI (Unit 9) can mirror it 1:1:
create-ish (crawl/ingest), run, process, review-packet, approve, reject,
backfill, list. The machine NEVER publishes — `backfill` only RECORDS a human's
paste + attestation (R26/R37)."""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path
from typing import Any

import click

from . import pipeline as pl
from .adapters.clock import now as _now
from .adapters.crawler.base import SourceSpec
from .adapters.crawler.factory import build_crawler
from .adapters.crawler.ingest import LocalIngestCrawler
from .adapters.publisher import signoff
from .adapters.publisher.review_packet import build_review_packet
from .adapters.storage import gossip_ingest as gi
from .adapters.storage.audit_log import AuditLog
from .adapters.storage.config_io import (
    find_config_example,
    init_workspace,
    load_config,
)
from .adapters.storage.job_store import JobStore
from .core.errors import EXIT_INTERNAL, EXIT_OK, DependencyError, LcpError, UsageError
from .core.models import SourceType
from .runtime_hardening import apply_hardening


def _completion_advisory(state: Any, *, dry_run: bool) -> str | None:
    """An operator-facing hint when a run did not reach a packet (Unit 5).

    dry-run never calls the LLM, so the copywriter sections stay empty and the
    draft cannot reach PROCESSED — say so plainly instead of a bare state."""
    from .core.state import JobState

    if dry_run and state is JobState.NEEDS_REVISION:
        return (
            "dry-run did not call the LLM, so image_sections/quick_facts/summary "
            "are empty and the draft cannot reach PROCESSED — re-run WITHOUT "
            "--dry-run (and with --ai-copy) for a complete review packet."
        )
    if state is JobState.NEEDS_REVISION:
        return (
            "draft parked for revision — see notes for the missing sections; a "
            "complete draft needs --ai-copy (and captions only for image bundles)."
        )
    return None


class Ctx:
    """Resolved per-invocation context: config + adapters, built once from flags."""

    def __init__(self, obj: dict):
        # Auto-discover ./config.yaml when no --config was given, so `lcp init`
        # (which writes config.yaml in cwd) is honoured by a plain `lcp run` —
        # reading where init writes. EXISTS-GATED on purpose: a bare
        # `or "config.yaml"` would make load_config raise "not found" on a fresh/CI
        # dir; only substitute when the file is actually present, else fall through
        # to defaults. An explicit --config is passed unchanged (and still raises if
        # missing). (The GUI does its own cwd resolution in webserver._make_api.)
        config_path = obj.get("config_path")
        # is_file (not exists): a *directory* or broken symlink named config.yaml
        # must fall through to defaults, not get handed to load_config -> read_text
        # (an IsADirectoryError would surface as exit 5, not a clean default).
        if not config_path and Path("config.yaml").is_file():
            config_path = "config.yaml"
        self.config = load_config(config_path)
        base_dir = obj.get("output_dir") or self.config.storage.base_dir
        self.store = JobStore(base_dir=base_dir)
        # Audit lives at the storage root (an EXTERNAL log survives per-job
        # deletion and still verifies — see job_store.delete_job docstring).
        self.audit = AuditLog(Path(base_dir) / "audit.jsonl")
        self.dry_run = bool(obj.get("dry_run"))
        self.as_json = bool(obj.get("as_json"))
        self.quiet = bool(obj.get("quiet"))

    def emit(self, payload: dict, *, human: str) -> None:
        """Print machine JSON or a human line, honouring --quiet for non-errors."""
        if self.as_json:
            click.echo(_json.dumps(payload, ensure_ascii=False, sort_keys=True))
        elif not self.quiet:
            click.echo(human)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.option("--dry-run", is_flag=True, help="Do not mutate external systems")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option("--verbose", is_flag=True, help="Verbose logging")
@click.option("--quiet", is_flag=True, help="Suppress non-error output")
@click.option("--output-dir", default=None, help="Override storage base dir")
@click.pass_context
def cli(ctx, config_path, dry_run, as_json, verbose, quiet, output_dir):
    """local-content-processor (lcp): crawl -> process -> review packet."""
    ctx.ensure_object(dict)
    ctx.obj.update(
        config_path=config_path,
        dry_run=dry_run,
        as_json=as_json,
        verbose=verbose,
        quiet=quiet,
        output_dir=output_dir,
    )


# --- Setup -------------------------------------------------------------------


@cli.command()
@click.pass_context
def init(ctx):
    """Scaffold a runnable workspace: config.yaml (0600) + an empty site index.

    Fixes the first-run blockers — without a config.yaml you have no reviewer
    whitelist/settings, and without a site_index.jsonl every clean job parks at
    the dedup honesty gate. Idempotent: never clobbers an existing config.yaml."""
    obj = ctx.obj
    config_path = Path(obj.get("config_path") or "config.yaml")
    if config_path.exists():
        base_dir = obj.get("output_dir") or load_config(str(config_path)).storage.base_dir
    else:
        base_dir = obj.get("output_dir") or "./data"
    created = init_workspace(
        config_path=config_path,
        example_path=find_config_example(),
        site_index_path=Path(base_dir) / "site_index.jsonl",
    )
    parts = []
    parts.append(
        f"wrote {config_path} (0600)"
        if created["config_created"]
        else f"kept existing {config_path}"
    )
    parts.append(
        "seeded empty site_index.jsonl"
        if created["index_created"]
        else "site index already present"
    )
    if obj.get("as_json"):
        click.echo(
            _json.dumps(
                {"config_path": str(config_path), **created},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif not obj.get("quiet"):
        click.echo(
            "init: "
            + "; ".join(parts)
            + ". Next: edit config.yaml (add a reviewer), then `lcp run`."
        )


# --- Stage 1: crawl / ingest -------------------------------------------------


@cli.command()
@click.option("--url", default=None, help="Single URL to crawl")
@click.option("--input", "input_file", default=None, help="URL list file")
@click.option(
    "--job-id",
    "job_id",
    required=True,
    help="New job id (a failed crawl can be retried in place; re-crawling an "
    "already-crawled job is refused — delete it first or use a fresh id)",
)
@click.pass_context
def crawl(ctx, url, input_file, job_id):
    """Stage 1: crawl a URL into a raw job bundle (Scrapy subprocess)."""
    c = Ctx(ctx.obj)
    if not url and not input_file:
        raise UsageError("crawl requires --url or --input")
    if input_file:
        # The URL-list file holds one URL per line. crawl creates exactly ONE
        # job (--job-id is single), so a multi-URL file is a footgun, not a
        # batch — fail loud instead of silently crawling only the first.
        urls = [
            ln.strip()
            for ln in Path(input_file).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        if not urls:
            raise UsageError(f"no URLs in {input_file}")
        if len(urls) > 1:
            raise UsageError(
                f"{input_file} has {len(urls)} URLs but crawl creates one job "
                "(--job-id is single). Crawl one URL per call, or use "
                "`ingest-gossip` for batch injection."
            )
        url = urls[0]

    # Route the URL crawl through Pipeline.stage1 (the single owner of the
    # create -> crawl -> map-status -> persist (state + hashes atomically)
    # sequence) using the shared CrawlRunnerCrawler adapter — no inline
    # re-implementation of Stage 1.
    crawler = build_crawler(c.config, c.audit, _now)
    spec = SourceSpec(
        job_id=job_id,
        source_type=SourceType.URL,
        job_dir=c.store.job_dir(job_id),
        url=url,
        max_assets=c.config.crawler.max_assets_per_job,
    )
    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run, crawler=crawler)
    res = p.stage1(spec, ts=_now())
    c.emit(
        {"job_id": job_id, "crawl_status": res.crawl_status, "state": res.record.state.value},
        human=f"crawled {job_id}: {res.crawl_status}",
    )


@cli.command()
@click.option("--dir", "directory", required=True, help="Local material folder")
@click.option(
    "--job-id",
    "job_id",
    required=True,
    help="New job id (re-ingesting an existing job is refused; "
    "delete/supersede it first or use a fresh id)",
)
@click.pass_context
def ingest(ctx, directory, job_id):
    """Stage 1: ingest a local material folder (no network)."""
    c = Ctx(ctx.obj)
    ts = _now()
    crawler = LocalIngestCrawler()
    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run, crawler=crawler)
    spec = SourceSpec(
        job_id=job_id,
        source_type=SourceType.LOCAL_DIR,
        job_dir=c.store.job_dir(job_id),
        local_dir=Path(directory),
        max_assets=c.config.crawler.max_assets_per_job,
    )
    rec = p.stage1(spec, ts=ts).record
    c.emit(
        {"job_id": job_id, "state": rec.state.value},
        human=f"ingested {job_id}: {rec.state.value}",
    )


@cli.command(name="ingest-gossip")
@click.option(
    "--input",
    "input_file",
    default=None,
    help="GossipItem JSON array file (reads stdin if omitted)",
)
@click.pass_context
def ingest_gossip(ctx, input_file):
    """Stage 1 bridge: create one job per GossipItem, persisting each source URL.

    Reads a GossipItem JSON array (from --input or stdin), creates one NEW job
    per valid item, and writes each item's source URL into the job bundle so a
    later `run --job-id` (no --url) can deep-crawl it. Invalid/duplicate items
    are skipped with a reason (non-lossy report); an oversized batch is refused."""
    c = Ctx(ctx.obj)
    raw = Path(input_file).read_text(encoding="utf-8") if input_file else sys.stdin.read()
    items = gi.parse_payload(raw)
    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run)
    report = p.ingest_gossip(items, ts=_now())
    c.emit(
        report.to_dict(),
        human=(f"ingest-gossip: created {len(report.created)}, skipped {len(report.skipped)}"),
    )


# --- Stage 2: process --------------------------------------------------------


@cli.command()
@click.option("--job-id", "job_id", default=None, help="Single job to process")
@click.option("--all-state", "all_state", default=None, help="Process ALL jobs in this state")
@click.option("--title", default="", help="Working title (lint/risk input)")
@click.option(
    "--watermark/--no-watermark",
    "watermark",
    default=None,
    help="Apply (or skip) the official watermark; default follows config",
)
@click.option(
    "--template",
    "template",
    default=None,
    help="Per-栏目 prompt template to apply (category name)",
)
@click.option(
    "--ai-copy",
    "ai_copy",
    is_flag=True,
    default=False,
    help="Also generate AI captions/FAQ/subheads (all need human review)",
)
@click.pass_context
def process(ctx, job_id, all_state, title, watermark, template, ai_copy):
    """Stage 2: risk gate, media validation/normalization, dedup gate, assemble,
    lint + ground.

    Honours --dry-run: the LLM is NOT called and no external system is mutated
    (the draft is marked not-executed; deterministic local stages incl. media
    still run). Stops at the first gate that parks the job (BLOCKED / DUPLICATE /
    NEEDS_*). --watermark/--template/--ai-copy are process-time inputs.

    Use --all-state <state> to batch-process every job in a given state
    (e.g. --all-state crawled). Each job is independent; parked/failed jobs
    do not stop the batch."""
    if not job_id and not all_state:
        raise click.UsageError("either --job-id or --all-state is required")
    if job_id and all_state:
        raise click.UsageError("cannot use both --job-id and --all-state")

    c = Ctx(ctx.obj)
    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run)

    if all_state:
        results = pl.process_batch(
            p,
            all_state,
            ts=_now(),
            title=title,
            ai_copy=ai_copy,
            watermark=watermark,
            template=template,
        )
        summary = {}
        for r in results:
            summary[r.final_state.value] = summary.get(r.final_state.value, 0) + 1
        c.emit(
            {
                "state": all_state,
                "processed": len(results),
                "summary": summary,
                "dry_run": p.dry_run,
            },
            human=(
                f"batch: processed {len(results)} jobs in state {all_state!r}: "
                + ", ".join(f"{k}={v}" for k, v in sorted(summary.items()))
                + (" [dry-run]" if p.dry_run else "")
            ),
        )
    else:
        res = p.process(
            job_id,
            ts=_now(),
            title=title,
            watermark=watermark,
            template=template,
            ai_copy=ai_copy,
        )
        c.emit(
            {
                "job_id": job_id,
                "state": res.final_state.value,
                "stopped_at": res.stopped_at,
                "dry_run": res.dry_run,
                "notes": res.notes,
            },
            human=(
                f"processed {job_id}: {res.final_state.value}"
                + (f" (stopped at {res.stopped_at})" if res.stopped_at else "")
                + (" [dry-run]" if res.dry_run else "")
                + (
                    f"\n  → {adv}"
                    if (adv := _completion_advisory(res.final_state, dry_run=res.dry_run))
                    else ""
                )
            ),
        )


# --- Stage 4: review packet (freeze) + sign-off ------------------------------


@cli.command(name="review-packet")
@click.option("--job-id", "job_id", required=True)
@click.option(
    "--source-url",
    "source_urls",
    multiple=True,
    help="Source URL(s) rendered as inert text in the packet",
)
@click.pass_context
def review_packet(ctx, job_id, source_urls):
    """Build the sanitized review packet and FREEZE the draft (PROCESSED ->
    REVIEW_PENDING). This is a HUMAN action (人), not auto."""
    c = Ctx(ctx.obj)
    # Freeze the EXACT draft Stage 2 produced (persisted by `process`/`run`) —
    # no re-assembly, so the packet never needs the LLM and the freeze hashes
    # bind the reviewed artifact.
    draft = pl.load_draft(c.store, job_id)
    if draft is None:
        raise UsageError(f"no processed draft for {job_id}; run `process` (or `run`) first")
    packet = build_review_packet(
        job_id=job_id,
        draft=draft,
        store=c.store,
        audit=c.audit,
        submitted_at=_now(),
        source_urls=list(source_urls),
    )
    c.emit(
        {
            "job_id": job_id,
            "state": "review_pending",
            "body_sha256": packet.body_sha256,
            "title_sha256": packet.title_sha256,
            "cover_sha256": packet.cover_sha256,
            "review_dir": str(packet.review_dir),
        },
        human=f"review packet built for {job_id}: REVIEW_PENDING (body {packet.body_sha256[:12]}…)",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--reviewer", required=True, help="Reviewer (must be whitelisted)")
@click.pass_context
def approve(ctx, job_id, reviewer):
    """Approve a REVIEW_PENDING job: REVIEW_PENDING -> APPROVED.

    Sign-off is ATTRIBUTION, not authentication (a verbatim disclaimer is
    recorded). The reviewer must be in config.publisher.reviewers. Does NOT
    publish — APPROVED is not complete until you `backfill` the URL (R37)."""
    c = Ctx(ctx.obj)
    # Load the persisted Stage-2 draft and pass it so signoff re-verifies the
    # frozen body hash — a draft tampered after freeze must NOT approve.
    draft = pl.load_draft(c.store, job_id)
    rec = signoff.approve(
        job_id,
        reviewer,
        config=c.config,
        store=c.store,
        audit=c.audit,
        ts=_now(),
        draft=draft,
    )
    c.emit(
        {
            "job_id": job_id,
            "state": rec.new_state.value,
            "reviewer_stated": rec.reviewer_stated,
            "observed_os_user": rec.observed_os_user,
            "body_sha256": rec.body_sha256,
            "disclaimer": rec.disclaimer,
        },
        human=f"approved {job_id} by {rec.reviewer_stated} "
        f"(os user {rec.observed_os_user}); not published until backfilled",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--reviewer", required=True, help="Reviewer (must be whitelisted)")
@click.option("--reason", required=True, help="Why it is rejected")
@click.pass_context
def reject(ctx, job_id, reviewer, reason):
    """Reject a REVIEW_PENDING job: REVIEW_PENDING -> REJECTED (terminal)."""
    c = Ctx(ctx.obj)
    rec = signoff.reject(
        job_id,
        reviewer,
        reason,
        config=c.config,
        store=c.store,
        audit=c.audit,
        ts=_now(),
    )
    c.emit(
        {"job_id": job_id, "state": rec.new_state.value, "reviewer_stated": rec.reviewer_stated},
        human=f"rejected {job_id} by {rec.reviewer_stated}",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--reviewer", required=True, help="Reviewer (must be whitelisted)")
@click.option(
    "--relint",
    is_flag=True,
    help="Re-run lint to clear a grounding hold (the human vouched for "
    "grounding); only a clean lint promotes to PROCESSED",
)
@click.option(
    "--reason", default=None, help="Required for a risk/dedup human override (recorded, audited)"
)
@click.pass_context
def resolve(ctx, job_id, reviewer, relint, reason):
    """Drive a NEEDS_HUMAN_REVIEW job out of the hold: -> PROCESSED.

    Grounding hold + --relint: lint re-runs; a clean lint promotes to PROCESSED.
    Risk/dedup hold (or grounding without --relint): an explicit --reason is
    required — this is a recorded human override (state machine allows it)."""
    c = Ctx(ctx.obj)
    rec = signoff.resolve(
        job_id,
        reviewer,
        config=c.config,
        store=c.store,
        audit=c.audit,
        ts=_now(),
        relint=relint,
        reason=reason,
    )
    c.emit(
        {"job_id": job_id, "state": rec.new_state.value, "reviewer_stated": rec.reviewer_stated},
        human=f"resolved {job_id} by {rec.reviewer_stated}: {rec.new_state.value}",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--reviewer", required=True, help="Reviewer (must be whitelisted)")
@click.option("--url", required=True, help="The published URL you pasted")
@click.option(
    "--attest/--no-attest",
    default=False,
    help="Confirm the published version IS the signed-off version",
)
@click.pass_context
def backfill(ctx, job_id, reviewer, url, attest):
    """Record a publish: APPROVED -> PUBLISHED_RECORDED (responsibility loop).

    The machine never publishes (R26). This only RECORDS that a whitelisted human
    pasted the URL AND ticked the attestation (--attest). Without --attest the job
    STAYS APPROVED (the loop is open, R37)."""
    c = Ctx(ctx.obj)
    new_state = signoff.backfill_published_url(
        job_id,
        url,
        config=c.config,
        store=c.store,
        audit=c.audit,
        ts=_now(),
        attested=attest,
        reviewer=reviewer,
    )
    c.emit(
        {"job_id": job_id, "state": new_state.value, "attested": attest},
        human=f"recorded publish for {job_id}: {new_state.value}",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option(
    "--new-job-id", "new_job_id", default=None, help="Back-link to the replacement job, if created"
)
@click.option(
    "--redline-override/--no-redline-override",
    "redline_override",
    default=False,
    help="REQUIRED second confirmation to recover a BLOCKED (redline) "
    "job. Without it a BLOCKED supersede is refused. Records a "
    "distinct REDLINE_OVERRIDE audit event (mirrors --attest).",
)
@click.pass_context
def supersede(ctx, job_id, new_job_id, redline_override):
    """Supersede a supersede-able job: -> SUPERSEDED (also the recovery seam).

    Ordinary abandon (REVIEW_PENDING/APPROVED/NEEDS_REVISION/NEEDS_HUMAN_REVIEW)
    and recovering a false-terminal DUPLICATE are single-step. Recovering a
    BLOCKED (redline) job requires --redline-override (a deliberate second
    confirmation, never an interactive prompt — it would hang on piped stdin) and
    records a distinct REDLINE_OVERRIDE event with the original blocking reasons.
    The actor recorded is the OBSERVED OS user, not a literal default."""
    c = Ctx(ctx.obj)
    new_state = signoff.supersede(
        job_id,
        store=c.store,
        audit=c.audit,
        ts=_now(),
        new_job_id=new_job_id,
        actor=signoff.observed_os_user(),
        redline_override=redline_override,
    )
    c.emit(
        {
            "job_id": job_id,
            "state": new_state.value,
            "new_job_id": new_job_id,
            "redline_override": bool(redline_override),
        },
        human=f"superseded {job_id}: {new_state.value}",
    )


# --- worklist + batch summary (G5/G7) ---------------------------------------


@cli.command(name="list")
@click.option(
    "--state", default=None, help="Filter: pending|blocked|needs-review|duplicate|approved|…"
)
@click.option("--summary", is_flag=True, help="Show counts-by-state summary")
@click.pass_context
def list_cmd(ctx, state, summary):
    """Pull-style worklist (G5/G7): list jobs, optionally filtered by state, or
    show the batch counts-by-state summary.

    The worklist is also the .processing crash-marker's consumer (U7): a
    reconciliation pass runs here so a job a crash interrupted mid-Stage-2 is
    flagged ``interrupted`` (with its crash-attempt count) the moment the operator
    looks at the worklist — it is surfaced for explicit re-process, never auto-run."""
    c = Ctx(ctx.obj)
    if summary:
        counts = pl.batch_summary(c.store)
        c.emit(
            {"summary": counts},
            human="\n".join(f"{k}: {v}" for k, v in counts.items()),
        )
        return
    interrupted = {
        i.job_id: i for i in pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run).reconcile()
    }
    records = pl.list_jobs(c.store, state)
    rows = [
        {
            "job_id": r.job_id,
            "state": r.state.value,
            "review_reason": r.review_reason.value if r.review_reason else None,
            "updated_at": r.updated_at,
            "interrupted": r.job_id in interrupted,
            "interrupt_attempts": (
                interrupted[r.job_id].attempts if r.job_id in interrupted else 0
            ),
            "interrupt_exhausted": (
                interrupted[r.job_id].exhausted if r.job_id in interrupted else False
            ),
        }
        for r in records
    ]
    c.emit(
        {"jobs": rows, "count": len(rows)},
        human=(
            "\n".join(
                f"{r['job_id']}\t{r['state']}"
                + (f"\t({r['review_reason']})" if r["review_reason"] else "")
                + (
                    "\t[INTERRUPTED"
                    + (" needs-human" if r["interrupt_exhausted"] else "")
                    + f" x{r['interrupt_attempts']}]"
                    if r["interrupted"]
                    else ""
                )
                for r in rows
            )
            or "(no jobs)"
        ),
    )


# --- run --until draft|review ------------------------------------------------


@cli.command()
@click.option("--url", default=None, help="URL to crawl (Stage 1)")
@click.option("--input", "input_dir", default=None, help="Local folder to ingest")
@click.option("--job-id", "job_id", required=True)
@click.option(
    "--until",
    "target",
    type=click.Choice([pl.TARGET_DRAFT, pl.TARGET_REVIEW]),
    default=pl.TARGET_DRAFT,
    help="Run up to draft or review",
)
@click.option("--title", default="", help="Working title")
@click.option(
    "--source-url",
    "source_urls",
    multiple=True,
    help="Source URL(s) for the review packet (inert text)",
)
@click.option(
    "--ai-copy/--no-ai-copy",
    default=True,
    help="Generate structural copy (captions/FAQ/quick-facts/summary/"
    "tags). ON by default — a complete draft needs it (D2).",
)
@click.option("--template", default=None, help="Per-栏目 prompt template to apply (category name)")
@click.pass_context
def run(ctx, url, input_dir, job_id, target, title, source_urls, ai_copy, template):
    """End-to-end run up to a target: Stage 1 -> Stage 2 (-> review packet).

    Use --url (crawl) or --input (local folder ingest). Honours --dry-run (LLM
    not called). --ai-copy is ON by default (a complete draft requires the
    copywriter sections); pass --no-ai-copy to skip. Stops early and reports the
    resting state if a gate parks the job (BLOCKED / DUPLICATE / NEEDS_*)."""
    c = Ctx(ctx.obj)
    if not url and not input_dir:
        # An ingested gossip job: its source URL was persisted at ingest time
        # (data/jobs/<id>/source.json). Read it so the crawl can proceed by id.
        url = gi.read_source_url(c.store.job_dir(job_id))
        if not url:
            raise UsageError(
                "run requires --url or --input (or an `ingest-gossip` job whose "
                "source URL was persisted)"
            )
    elif bool(url) and bool(input_dir):
        raise UsageError("run requires exactly one of --url or --input")

    if url:
        crawler = build_crawler(c.config, c.audit, _now)
        spec = SourceSpec(
            job_id=job_id,
            source_type=SourceType.URL,
            job_dir=c.store.job_dir(job_id),
            url=url,
            max_assets=c.config.crawler.max_assets_per_job,
        )
    else:
        crawler = LocalIngestCrawler()
        spec = SourceSpec(
            job_id=job_id,
            source_type=SourceType.LOCAL_DIR,
            job_dir=c.store.job_dir(job_id),
            local_dir=Path(input_dir),
            max_assets=c.config.crawler.max_assets_per_job,
        )

    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run, crawler=crawler)
    res = p.run_until(
        spec,
        target=target,
        ts=_now(),
        title=title,
        source_urls=list(source_urls),
        ai_copy=ai_copy,
        template=template,
    )
    c.emit(
        {
            "job_id": job_id,
            "state": res.final_state.value,
            "target": res.target,
            "dry_run": res.dry_run,
            "notes": res.notes,
            "body_sha256": res.packet.body_sha256 if res.packet else None,
        },
        human=f"run {job_id} --until {target}: {res.final_state.value}"
        + (" [dry-run]" if res.dry_run else "")
        + (
            f"\n  → {adv}"
            if (adv := _completion_advisory(res.final_state, dry_run=res.dry_run))
            else ""
        ),
    )


@cli.command(name="show-ingest-report")
@click.option("--job-id", "job_id", required=True)
@click.pass_context
def show_ingest_report(ctx, job_id):
    """Print the LOCAL_DIR ingest completeness report for a job (SOP step 01).

    Exits 0 when the report is complete; exits 2 when absent or incomplete."""
    import json as _json_mod

    from .adapters.crawler.net_guard import safe_join

    c = Ctx(ctx.obj)
    raw_dir = Path(c.store.job_dir(job_id)) / "raw"
    try:
        report_path = safe_join(raw_dir, "ingest_report.json")
    except LcpError as e:
        raise click.ClickException(str(e)) from e
    if not report_path.exists():
        c.emit({"job_id": job_id, "report": None}, human=f"no ingest report for {job_id}")
        raise SystemExit(2)
    data = _json_mod.loads(report_path.read_text(encoding="utf-8"))
    if c.as_json:
        click.echo(_json_mod.dumps(data, ensure_ascii=False, sort_keys=True))
        return
    imgs = data.get("imported_images", 0)
    vids = data.get("imported_videos", 0)
    failed = data.get("failed") or []
    click.echo(f"✓ 已匯入 {imgs} 張圖片、{vids} 支影片")
    if not data.get("has_body"):
        click.echo("⚠ 缺少正文 (body.txt)")
    if failed:
        names = ", ".join(f["name"] for f in failed[:5])
        click.echo(f"⚠ {len(failed)} 個檔案匯入失敗: {names}")
    if not data.get("complete"):
        raise SystemExit(2)


@cli.command()
@click.option("--job-id", "job_id", required=True, help="REVIEW_PENDING job to notify for")
@click.pass_context
def notify(ctx, job_id):
    """Send cover + title to the configured Telegram group (SOP step 10).

    Job must be REVIEW_PENDING. Fire-and-forget: failure is audited but never
    parks the job. Bot token from keyring (service=local-content-processor,
    user=tg_bot) or LCP_TG_BOT_TOKEN env var. Configure chat_id in config.yaml."""
    from .adapters.publisher import notifier as _notifier
    from .adapters.storage.config_io import resolve_tg_bot_token

    c = Ctx(ctx.obj)
    review_dir = Path(c.store.base_dir) / "jobs" / job_id / "review_packet"
    # Load draft title from job store for caption; empty string if not yet built.
    draft = pl.load_draft(c.store, job_id)
    title = draft.title if draft else ""
    bot_token = resolve_tg_bot_token()
    _notifier.send_notification(
        job_id,
        review_dir,
        title,
        c.config.notification,
        c.audit,
        c.store,
        bot_token=bot_token,
        ts=_now(),
        dry_run=c.dry_run,
    )
    c.emit(
        {"job_id": job_id, "notified": True, "dry_run": c.dry_run},
        human=f"notification sent for {job_id}"
        if not c.dry_run
        else f"dry-run: notification skipped for {job_id}",
    )


@cli.command(name="set-tg-token")
def set_tg_token():
    """Store the Telegram bot token in the OS keyring (interactive, reads from stdin).

    The token is read via getpass (no echo) so it never appears in shell history
    or process listing. Stored in keyring service=local-content-processor, user=tg_bot."""
    import getpass

    from .adapters.storage.config_io import set_tg_bot_token

    token = getpass.getpass("Telegram bot token: ")
    set_tg_bot_token(token)
    click.echo("Telegram bot token saved to OS keyring.")


@cli.command()
@click.option("--port", type=int, default=None, help="Port to bind on 127.0.0.1 (default 8765).")
@click.option(
    "--no-browser", is_flag=True, help="Do not auto-open the browser; just print the URL."
)
@click.pass_context
def gui(ctx, port, no_browser):
    """Launch the local webui: a 127.0.0.1 HTTP service serving the operator UI.

    Prints (and, by default, opens) a http://127.0.0.1:PORT/ URL — open it in
    Chrome to drive/debug with Claude in Chrome. Stdlib only; no extra deps.
    Configure the LLM endpoint + api_key from the Settings panel; base_url/model
    go to the config file, the api_key goes to the OS keyring only (never a file)."""
    from .webserver import DEFAULT_PORT, serve

    try:
        serve(
            config_path=ctx.obj.get("config_path"),
            port=port if port is not None else DEFAULT_PORT,
            open_browser=not no_browser,
        )
    except OSError as e:  # e.g. port already in use (EADDRINUSE)
        raise DependencyError(
            f"could not start the webui server (port in use?): {e}; try --port"
        ) from e


def main(argv: list[str] | None = None) -> int:
    """Entry point. Maps LcpError -> exit_code; unexpected -> EXIT_INTERNAL."""
    apply_hardening()
    try:
        cli.main(args=argv, standalone_mode=False)
        return EXIT_OK
    except click.UsageError as e:
        click.echo(str(e), err=True)
        return 1
    except click.ClickException as e:
        e.show()
        return 1
    except LcpError as e:
        click.echo(f"error: {e}", err=True)
        return e.exit_code
    except click.exceptions.Abort:
        return 1
    except Exception as e:  # noqa: BLE001 - shell boundary
        click.echo(f"internal error: {e}", err=True)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
