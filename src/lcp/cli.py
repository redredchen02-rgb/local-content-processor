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

import datetime as _dt
import json as _json
import sys
from pathlib import Path

import click

from . import pipeline as pl
from .adapters.crawler.base import SourceSpec
from .adapters.crawler.crawl_runner import CrawlRunner
from .adapters.crawler.ingest import LocalIngestCrawler
from .adapters.crawler.source_registry import SourceRegistry
from .adapters.publisher import signoff
from .adapters.publisher.review_packet import build_review_packet
from .adapters.storage.audit_log import AuditLog
from .adapters.storage.job_store import JobStore
from .core.config import load_config
from .core.errors import EXIT_INTERNAL, EXIT_OK, LcpError, UsageError
from .core.models import SourceType
from .runtime_hardening import apply_hardening


def _now() -> str:
    """ISO8601 UTC timestamp. The CLI is the I/O boundary, so generating the
    timestamp here (not in core/adapters) keeps the lower layers deterministic."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ctx:
    """Resolved per-invocation context: config + adapters, built once from flags."""

    def __init__(self, obj: dict):
        self.config = load_config(obj.get("config_path"))
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


# --- Stage 1: crawl / ingest -------------------------------------------------


@cli.command()
@click.option("--url", default=None, help="Single URL to crawl")
@click.option("--input", "input_file", default=None, help="URL list file")
@click.option("--job-id", "job_id", required=True, help="Job id to create/use")
@click.pass_context
def crawl(ctx, url, input_file, job_id):
    """Stage 1: crawl a URL into a raw job bundle (Scrapy subprocess)."""
    c = Ctx(ctx.obj)
    if not url and not input_file:
        raise UsageError("crawl requires --url or --input")
    if input_file:
        # MVP: the URL-list path is read by the operator; we crawl the first URL
        # entry here and leave batch fan-out to repeated invocations / cron.
        urls = [
            ln.strip()
            for ln in Path(input_file).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        if not urls:
            raise UsageError(f"no URLs in {input_file}")
        url = urls[0]

    registry = SourceRegistry.from_config(c.config.crawler)
    runner = CrawlRunner(
        registry,
        timeout=c.config.crawler.timeout_seconds,
        audit=c.audit,
    )
    ts = _now()
    rec = c.store.get_job(job_id)
    if rec is None:
        c.store.create_job(job_id, created_at=ts)
    spec = SourceSpec(
        job_id=job_id,
        source_type=SourceType.URL,
        job_dir=c.store.job_dir(job_id),
        url=url,
        max_assets=c.config.crawler.max_assets_per_job,
    )
    bundle = runner.crawl_url(spec, ts=ts)
    target = pl._CRAWL_STATUS_TO_STATE.get(bundle.job_status)
    if target is not None:
        c.store.set_hashes(
            job_id,
            updated_at=ts,
            source_html_sha256=bundle.manifest.hashes.source_html_sha256,
            source_text_sha256=bundle.manifest.hashes.source_text_sha256,
        )
        c.store.set_state(job_id, target, updated_at=ts)
    c.emit(
        {"job_id": job_id, "crawl_status": bundle.job_status,
         "state": target.value if target else bundle.job_status},
        human=f"crawled {job_id}: {bundle.job_status}",
    )


@cli.command()
@click.option("--dir", "directory", required=True, help="Local material folder")
@click.option("--job-id", "job_id", required=True, help="Job id to create/use")
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
    rec = p.stage1(spec, ts=ts)
    c.emit(
        {"job_id": job_id, "state": rec.state.value},
        human=f"ingested {job_id}: {rec.state.value}",
    )


# --- Stage 2: process --------------------------------------------------------


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--title", default="", help="Working title (lint/risk input)")
@click.pass_context
def process(ctx, job_id, title):
    """Stage 2: validate media, risk + dedup gates, assemble, lint + ground.

    Honours --dry-run: the LLM is NOT called and no external system is mutated
    (the draft is marked not-executed). Stops at the first gate that parks the
    job (BLOCKED / DUPLICATE / NEEDS_*)."""
    c = Ctx(ctx.obj)
    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run)
    res = p.process(job_id, ts=_now(), title=title)
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
        ),
    )


# --- Stage 4: review packet (freeze) + sign-off ------------------------------


@cli.command(name="review-packet")
@click.option("--job-id", "job_id", required=True)
@click.option("--source-url", "source_urls", multiple=True,
              help="Source URL(s) rendered as inert text in the packet")
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
        raise UsageError(
            f"no processed draft for {job_id}; run `process` (or `run`) first"
        )
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
        human=f"review packet built for {job_id}: REVIEW_PENDING "
        f"(body {packet.body_sha256[:12]}…)",
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
    rec = signoff.approve(
        job_id, reviewer,
        config=c.config, store=c.store, audit=c.audit, ts=_now(),
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
        job_id, reviewer, reason,
        config=c.config, store=c.store, audit=c.audit, ts=_now(),
    )
    c.emit(
        {"job_id": job_id, "state": rec.new_state.value,
         "reviewer_stated": rec.reviewer_stated},
        human=f"rejected {job_id} by {rec.reviewer_stated}",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--url", required=True, help="The published URL you pasted")
@click.option("--attest/--no-attest", default=False,
              help="Confirm the published version IS the signed-off version")
@click.pass_context
def backfill(ctx, job_id, url, attest):
    """Record a publish: APPROVED -> PUBLISHED_RECORDED (responsibility loop).

    The machine never publishes (R26). This only RECORDS that a human pasted the
    URL AND ticked the attestation (--attest). Without --attest the job STAYS
    APPROVED (the loop is open, R37)."""
    c = Ctx(ctx.obj)
    new_state = signoff.backfill_published_url(
        job_id, url,
        store=c.store, audit=c.audit, ts=_now(), attested=attest,
    )
    c.emit(
        {"job_id": job_id, "state": new_state.value, "attested": attest},
        human=f"recorded publish for {job_id}: {new_state.value}",
    )


@cli.command()
@click.option("--job-id", "job_id", required=True)
@click.option("--new-job-id", "new_job_id", default=None,
              help="Back-link to the replacement job, if created")
@click.pass_context
def supersede(ctx, job_id, new_job_id):
    """Supersede a REVIEW_PENDING/APPROVED/NEEDS_REVISION job: -> SUPERSEDED.

    Voids the old sign-off (SIGNOFF_INVALIDATED) and back-links the new job."""
    c = Ctx(ctx.obj)
    new_state = signoff.supersede(
        job_id, store=c.store, audit=c.audit, ts=_now(), new_job_id=new_job_id,
    )
    c.emit(
        {"job_id": job_id, "state": new_state.value, "new_job_id": new_job_id},
        human=f"superseded {job_id}: {new_state.value}",
    )


# --- worklist + batch summary (G5/G7) ---------------------------------------


@cli.command(name="list")
@click.option("--state", default=None,
              help="Filter: pending|blocked|needs-review|duplicate|approved|…")
@click.option("--summary", is_flag=True, help="Show counts-by-state summary")
@click.pass_context
def list_cmd(ctx, state, summary):
    """Pull-style worklist (G5/G7): list jobs, optionally filtered by state, or
    show the batch counts-by-state summary."""
    c = Ctx(ctx.obj)
    if summary:
        counts = pl.batch_summary(c.store)
        c.emit(
            {"summary": counts},
            human="\n".join(f"{k}: {v}" for k, v in counts.items()),
        )
        return
    records = pl.list_jobs(c.store, state)
    rows = [
        {"job_id": r.job_id, "state": r.state.value,
         "review_reason": r.review_reason.value if r.review_reason else None,
         "updated_at": r.updated_at}
        for r in records
    ]
    c.emit(
        {"jobs": rows, "count": len(rows)},
        human=(
            "\n".join(
                f"{r['job_id']}\t{r['state']}"
                + (f"\t({r['review_reason']})" if r["review_reason"] else "")
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
@click.option("--until", "target", type=click.Choice([pl.TARGET_DRAFT, pl.TARGET_REVIEW]),
              default=pl.TARGET_DRAFT, help="Run up to draft or review")
@click.option("--title", default="", help="Working title")
@click.option("--source-url", "source_urls", multiple=True,
              help="Source URL(s) for the review packet (inert text)")
@click.pass_context
def run(ctx, url, input_dir, job_id, target, title, source_urls):
    """End-to-end run up to a target: Stage 1 -> Stage 2 (-> review packet).

    Use --url (crawl) or --input (local folder ingest). Honours --dry-run (LLM
    not called). Stops early and reports the resting state if a gate parks the
    job (BLOCKED / DUPLICATE / NEEDS_*)."""
    c = Ctx(ctx.obj)
    if bool(url) == bool(input_dir):
        raise UsageError("run requires exactly one of --url or --input")

    if url:
        registry = SourceRegistry.from_config(c.config.crawler)
        crawler = CrawlRunnerCrawler(
            CrawlRunner(registry, timeout=c.config.crawler.timeout_seconds,
                        audit=c.audit),
            ts_provider=_now,
        )
        spec = SourceSpec(
            job_id=job_id, source_type=SourceType.URL,
            job_dir=c.store.job_dir(job_id), url=url,
            max_assets=c.config.crawler.max_assets_per_job,
        )
    else:
        crawler = LocalIngestCrawler()
        spec = SourceSpec(
            job_id=job_id, source_type=SourceType.LOCAL_DIR,
            job_dir=c.store.job_dir(job_id), local_dir=Path(input_dir),
            max_assets=c.config.crawler.max_assets_per_job,
        )

    p = pl.Pipeline(c.config, c.store, c.audit, dry_run=c.dry_run, crawler=crawler)
    res = p.run_until(
        spec, target=target, ts=_now(), title=title,
        source_urls=list(source_urls),
    )
    c.emit(
        {
            "job_id": job_id, "state": res.final_state.value,
            "target": res.target, "dry_run": res.dry_run, "notes": res.notes,
            "body_sha256": res.packet.body_sha256 if res.packet else None,
        },
        human=f"run {job_id} --until {target}: {res.final_state.value}"
        + (" [dry-run]" if res.dry_run else ""),
    )


class CrawlRunnerCrawler:
    """Adapt the URL CrawlRunner (which needs a ts) to the Crawler contract so
    Pipeline.run_until can drive the network crawl path the same way as ingest."""

    def __init__(self, runner: CrawlRunner, *, ts_provider):
        self._runner = runner
        self._ts = ts_provider

    def crawl(self, spec: SourceSpec):
        return self._runner.crawl_url(spec, ts=self._ts())


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
