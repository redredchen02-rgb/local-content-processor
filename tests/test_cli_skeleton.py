"""CLI shell tests: help, exit-code mapping, and that the commands are now wired.

Unit 8 implemented the commands the skeleton previously stubbed. We keep the
exit-code contract (main() reads error.exit_code) and the help listing, and add
coverage that the now-implemented commands parse and route to the pipeline."""

import yaml

from lcp.cli import main
from lcp.core.errors import EXIT_INPUT, EXIT_OK, EXIT_USAGE


def test_help_lists_commands(capsys):
    rc = main(["--help"])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    # Original Stage-1 commands still present...
    assert "crawl" in out and "ingest" in out
    # ...plus the Unit 8 operator actions (CLI/GUI parity surface).
    for cmd in ("process", "review-packet", "approve", "reject", "backfill",
                "list", "run", "supersede", "resolve"):
        assert cmd in out


def test_missing_required_option_is_usage_error(capsys):
    # crawl now requires --job-id; missing it is a click usage error (exit 1).
    rc = main(["crawl", "--url", "https://example.com/p/1"])
    assert rc == EXIT_USAGE


def test_crawl_off_allowlist_is_input_error(tmp_path, monkeypatch, capsys):
    # No config -> empty allowlist -> example.com is rejected (exit 2), proving
    # the command is wired to the runner's preflight (not a stub).
    # chdir to a clean dir so cwd config.yaml auto-discovery (Ctx) can't supply a
    # non-default allowlist — the rejection must be for the EMPTY-default reason,
    # not because a stray local config.yaml happens to exclude example.com.
    monkeypatch.chdir(tmp_path)
    rc = main([
        "--output-dir", str(tmp_path),
        "crawl", "--url", "https://example.com/p/1", "--job-id", "j1",
    ])
    assert rc == EXIT_INPUT


def test_no_command_shows_help_like_behaviour(capsys):
    rc = main([])
    # click group with no subcommand returns non-zero usage, never crashes
    assert rc in (EXIT_OK, EXIT_USAGE)


def _processed_job_with_draft(base, job_id="j1"):
    """Drive a job to PROCESSED with a persisted draft, out-of-band (the gates
    are covered in tests/test_pipeline_batch.py)."""
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.draft import Draft, FaqItem, SourceQuote
    from lcp.core.state import JobState
    from lcp.pipeline import save_draft

    ts = "2026-06-16T00:00:00Z"
    store = JobStore(base_dir=base)
    store.create_job(job_id, created_at=ts)
    store.set_state(job_id, JobState.CRAWLED, updated_at=ts)
    raw = store.job_dir(job_id) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "source.txt").write_text("華山文創園區本週末舉辦美食市集。", encoding="utf-8")
    draft = Draft(
        title="台北華山美食市集週末熱鬧登場活動", intro="引言。",
        quick_facts=["週末"], event_body="華山文創園區本週末舉辦美食市集。",
        faq=[FaqItem(question="Q", answer="A")], summary="結尾。",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    save_draft(store, job_id, draft)
    persist_gate_state(store, job_id, JobState.PROCESSED, updated_at=ts)
    return store


def test_full_signoff_loop_via_cli(tmp_path):
    """review-packet -> approve (whitelist) -> backfill (attest) through the CLI,
    proving CLI/GUI parity for every operator action (Unit 9 mirrors these)."""
    base = str(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": base}, "publisher": {"reviewers": ["alice"]}}
        ),
        encoding="utf-8",
    )
    store = _processed_job_with_draft(base, "j1")

    # 1. review-packet freezes the draft -> REVIEW_PENDING.
    assert main(["--config", str(cfg), "review-packet", "--job-id", "j1"]) == EXIT_OK
    from lcp.core.state import JobState

    assert store.get_job("j1").state is JobState.REVIEW_PENDING

    # 2. non-whitelisted reviewer -> input error, no transition.
    assert main(["--config", str(cfg), "approve", "--job-id", "j1",
                 "--reviewer", "mallory"]) == EXIT_INPUT
    assert store.get_job("j1").state is JobState.REVIEW_PENDING

    # 3. whitelisted approve -> APPROVED.
    assert main(["--config", str(cfg), "approve", "--job-id", "j1",
                 "--reviewer", "alice"]) == EXIT_OK
    assert store.get_job("j1").state is JobState.APPROVED

    # 4. backfill without --attest stays APPROVED (loop open).
    assert main(["--config", str(cfg), "backfill", "--job-id", "j1",
                 "--reviewer", "alice",
                 "--url", "https://site.example/x"]) == EXIT_INPUT
    assert store.get_job("j1").state is JobState.APPROVED

    # 5. backfill with --attest -> PUBLISHED_RECORDED.
    assert main(["--config", str(cfg), "backfill", "--job-id", "j1",
                 "--reviewer", "alice",
                 "--url", "https://site.example/x", "--attest"]) == EXIT_OK
    assert store.get_job("j1").state is JobState.PUBLISHED_RECORDED


def test_cli_approve_rejects_body_tampered_after_freeze(tmp_path):
    """P1 regression: approve via the CLI (no draft= arg) must load the persisted
    draft and re-verify the frozen body hash. Overwriting draft.json with a
    different body after the freeze -> approval FAILS and the job stays
    REVIEW_PENDING (never reaches APPROVED)."""
    from lcp.core.draft import Draft, FaqItem, SourceQuote
    from lcp.core.state import JobState
    from lcp.pipeline import save_draft

    base = str(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": base}, "publisher": {"reviewers": ["alice"]}}
        ),
        encoding="utf-8",
    )
    store = _processed_job_with_draft(base, "jt")
    # Freeze the packet at the original draft.
    assert main(["--config", str(cfg), "review-packet", "--job-id", "jt"]) == EXIT_OK
    assert store.get_job("jt").state is JobState.REVIEW_PENDING

    # Tamper: overwrite the persisted draft.json with a different body.
    tampered = Draft(
        title="台北華山美食市集週末熱鬧登場活動", intro="引言。",
        quick_facts=["週末"], event_body="完全不同的正文，已被竄改。",
        faq=[FaqItem(question="Q", answer="A")], summary="結尾。",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    save_draft(store, "jt", tampered)

    # Approve must FAIL (hash mismatch) and NOT transition.
    rc = main(["--config", str(cfg), "approve", "--job-id", "jt",
               "--reviewer", "alice"])
    assert rc == EXIT_INPUT
    assert store.get_job("jt").state is JobState.REVIEW_PENDING


def test_cli_resolve_drives_nhr_to_processed(tmp_path):
    """P1 regression: a NEEDS_HUMAN_REVIEW (risk) job is no longer stuck — the
    CLI `resolve` command with a reason overrides it to PROCESSED."""
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState, ReviewReason

    base = str(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": base}, "publisher": {"reviewers": ["alice"]}}
        ),
        encoding="utf-8",
    )
    ts = "2026-06-16T00:00:00Z"
    store = JobStore(base_dir=base)
    store.create_job("jn", created_at=ts)
    store.set_state("jn", JobState.CRAWLED, updated_at=ts)
    persist_gate_state(store, "jn", JobState.NEEDS_HUMAN_REVIEW, updated_at=ts,
                       review_reason=ReviewReason.RISK)

    # override without a reason -> input error (honest: override needs a reason)
    assert main(["--config", str(cfg), "resolve", "--job-id", "jn",
                 "--reviewer", "alice"]) == EXIT_INPUT
    assert store.get_job("jn").state is JobState.NEEDS_HUMAN_REVIEW

    # override with a reason -> PROCESSED
    assert main(["--config", str(cfg), "resolve", "--job-id", "jn",
                 "--reviewer", "alice", "--reason", "false positive"]) == EXIT_OK
    assert store.get_job("jn").state is JobState.PROCESSED


def test_cli_blocked_recovery_requires_redline_override(tmp_path):
    """U8 CLI parity: a BLOCKED supersede WITHOUT --redline-override is refused
    (exit 2); WITH it the job recovers to SUPERSEDED. A DUPLICATE needs only the
    ordinary supersede (no flag)."""
    from lcp.adapters.processor._persist import persist_gate_state
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    base = str(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"storage": {"base_dir": base}, "publisher": {"reviewers": ["alice"]}}
        ),
        encoding="utf-8",
    )
    ts = "2026-06-16T00:00:00Z"
    store = JobStore(base_dir=base)
    for jid, st in (("jb", JobState.BLOCKED), ("jd", JobState.DUPLICATE)):
        store.create_job(jid, created_at=ts)
        store.set_state(jid, JobState.CRAWLED, updated_at=ts)
        persist_gate_state(store, jid, st, updated_at=ts)

    # BLOCKED without the override flag -> refused (exit 2), state unchanged.
    assert main(["--config", str(cfg), "supersede", "--job-id", "jb"]) == EXIT_INPUT
    assert store.get_job("jb").state is JobState.BLOCKED
    # BLOCKED with --redline-override -> recovered.
    assert main(["--config", str(cfg), "supersede", "--job-id", "jb",
                 "--redline-override"]) == EXIT_OK
    assert store.get_job("jb").state is JobState.SUPERSEDED
    # DUPLICATE needs only the ordinary single-step supersede.
    assert main(["--config", str(cfg), "supersede", "--job-id", "jd"]) == EXIT_OK
    assert store.get_job("jd").state is JobState.SUPERSEDED


def test_review_packet_without_draft_is_usage_error(tmp_path, monkeypatch):
    base = str(tmp_path)
    from lcp.adapters.storage.job_store import JobStore
    from lcp.core.state import JobState

    # chdir to a clean dir: with cwd config.yaml auto-discovery, this no---config
    # command must stay independent of a stray local config.yaml.
    monkeypatch.chdir(tmp_path)
    store = JobStore(base_dir=base)
    store.create_job("j2", created_at="2026-06-16T00:00:00Z")
    store.set_state("j2", JobState.CRAWLED, updated_at="2026-06-16T00:00:00Z")
    rc = main(["--output-dir", base, "review-packet", "--job-id", "j2"])
    assert rc == EXIT_USAGE
