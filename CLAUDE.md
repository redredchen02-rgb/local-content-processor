# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`local-content-processor` (`lcp`) is a **local content pipeline**: `crawl/ingest → process → review packet`, and it **deliberately stops before publishing**. A human reviews the frozen packet and publishes by hand; the machine never writes to a CMS. It is own-site, compliance-first (domain allowlists, robots.txt, SSRF guards), and driven by a non-technical operator via a CLI with a 1:1 GUI mirror. See `README.md` for the operator-facing usage; this file is the architectural orientation.

## Commands

There is no Makefile or task runner — everything runs through the project venv (`./.venv/bin/...`). Setup: `python3.11 -m venv .venv && ./.venv/bin/pip install -e ".[crawl,media,llm,dedup,gui,dev]"`. Requires Python 3.11+ and `ffmpeg`/`ffprobe` on `PATH`.

```sh
./.venv/bin/python -m pytest -q                          # full suite (~600 tests)
./.venv/bin/python -m pytest tests/test_state_machine.py # one file
./.venv/bin/python -m pytest tests/processor -q          # one subdir (mirrors src/)
./.venv/bin/python -m pytest -k grounding -q             # by keyword
./.venv/bin/mypy                                         # the type gate — files come from pyproject
```

CI (`.github/workflows/ci.yml`) is exactly: install `.[crawl,media,llm,dedup,dev]` (no `gui`), then **`mypy`** (bare — config in `pyproject`), then **`pytest -q`**. Both must be green to merge.

- **Run mypy from `.venv`, never pyenv.** A stale system Pillow produces false positives. The venv has the CI-matching deps; trust only that result.
- The detection-accuracy spike is a **mechanics harness**, not a go/no-go decider: `./.venv/bin/python spikes/detection_accuracy/run_eval.py [--json]`.

## Architecture: functional core / imperative shell

The single most important structural rule. Read `src/lcp/pipeline.py`'s module docstring before changing pipeline behavior.

- **`src/lcp/core/`** — pure functional core. **No I/O, no framework.** Models, rules (risk/dedup/lint/grounding), draft, and the state machine. All *business judgement* lives here.
- **`src/lcp/adapters/`** — the imperative shell: `crawler/`, `media/`, `llm/`, `processor/` (Stage-2 gates), `publisher/`, `storage/`. These do I/O and call into the pure core.
- **`src/lcp/pipeline.py`** — the injection seam. Holds the injected adapters (store, audit, crawler, llm client) + config, runs the stages, drives the state machine. CLI/GUI build a `Pipeline` and call it.
- **`src/lcp/cli.py` + `src/lcp/gui.py`** — thin shells only (Click / pywebview glue). They mirror each other 1:1; **any operator action added to one must exist in the other.** Keep them logic-free.

When adding a stage or gate: put the decision in `core/`, the I/O in an `adapters/` module, wire it in `pipeline.py`, expose it in *both* shells.

## The job state machine is the source of truth

`src/lcp/core/state.py` holds the transition table — the only authority on legal lifecycle moves. Happy path:

```
NEW → CRAWLED → PROCESSING → PROCESSED → REVIEW_PENDING → APPROVED → PUBLISHED_RECORDED
```

Two invariants that constrain how you write code here:

- **Freeze is enforced by edge ABSENCE, not a guard.** There is intentionally no `REVIEW_PENDING → PROCESSING` edge — once a review packet is frozen the draft is immutable. Do not add that edge to "fix" anything.
- **`PROCESSING` is transient and never persisted to SQLite.** Crash detection relies on a `.processing` marker file instead, consumed by `Pipeline.reconcile()` (the worklist `list`/GUI `list_jobs` boundary), which flags a job a crash left mid-Stage-2 as **interrupted** for explicit operator re-process — never auto-running it — and carries a per-job-dir `.interrupt_count` so a deterministic crash surfaces to a human after N passes instead of looping. Because `PROCESSING` is transient, Stage-2 gates cannot persist `PROCESSING → target` directly — they go through `adapters/processor/_persist.py::persist_gate_state` (→ `JobStore.persist_from_processing`), which validates the canonical `persisted_current → PROCESSING → target` edge via the pure state machine, persists the resting state, and clears the marker. The marker is **caller-owned**: `persist_from_processing` does *not* require or assert it (asserting would pull marker I/O under the WAL write lock and break the `PROCESS_FAILED` retry path) — `Pipeline.process` sets it at Stage-2 entry; the seam only clears it after commit. Always use this seam to land a gate's resting state.

## Stage 2 is a fail-closed gate chain

`Pipeline.process` runs gates in a fixed order and **stops at the first one that parks the job** (see `_process_inner`):

```
risk → media → dedup → assemble (LLM) → lint + grounding
```

Order matters by design: the cheap terminal risk hard-stop runs first (redline content never spends media/LLM work); media validation runs before the LLM (bad media never spends tokens). Each gate that fails **fail-closed** — it parks the job at a hold state (`BLOCKED`/`DUPLICATE`/`NEEDS_HUMAN_REVIEW`/`NEEDS_REVISION`) for a human rather than auto-passing. `BLOCKED` and `DUPLICATE` are terminal. An `ExternalServiceError` (LLM 5xx/timeout) maps to `PROCESS_FAILED` (retriable), never silently left at `CRAWLED`.

`dry_run` (`-R32`): deterministic local stages still run, but the `LlmClient` is built with `dry_run=True` and **never calls the API**. An injected live client cannot override this — `Pipeline.__init__` forces dry mode on or refuses. Preserve that guarantee.

## Subprocess isolation for heavy/untrusted work

The **crawler** (`adapters/crawler/crawl_runner.py`) shells out to a **subprocess with a scrubbed environment** (`runtime_hardening.minimal_env`) — one Scrapy subprocess per job, secrets stripped from its env — so its heavy deps and blast radius stay isolated from the main process. This is the template for any future isolated engine that needs heavy or untrusted dependencies.

> A de-watermark / inpaint engine (Batch 2) once lived here as a second example; it was **CUT** on 2026-06-17 (see `docs/plans/2026-06-17-003-refactor-cut-dewatermark-pipeline-plan.md`). The pipeline ships no watermark-removal path.

## Security & compliance invariants (these constrain every change)

These are load-bearing, not aspirational. `docs/security/pii-inventory.md` is the reference.

- **No publish without a human.** `approve` is attribution, not publication; `backfill --attest` only records a human's pasted URL + attestation.
- **The SQLite index, manifest, and audit log are PII-free by construction** — hashes and enum codes only, never free text. `ReviewReason` is stored as its code. Keep it that way when adding fields.
- **PII at rest is plaintext, `0600`.** `apply_hardening()` sets umask `0o077` at startup (call before any file write / subprocess spawn). Job bundles live under `data/jobs/<id>/` (gitignored; never commit `data/`).
- **LLM is zero-capability.** A single Chat Completions call returning text — no tools, no link-following, no writes. Attacker-shapeable fields are HTML-escaped; source URLs render as **inert text**, never a live `<a href>` and never fetched (on both the packet and the GUI bridge).
- **Secrets never live in `config.yaml`.** LLM API key comes from the OS keyring (service `local-content-processor`, user `llm`) or `LCP_LLM_API_KEY`. `config.yaml` is gitignored; copy from `config.example.yaml`.
- **SSRF guards:** scheme allowlist, DNS `is_global` check on the top URL *and* every scraped media URL, Scrapy `allowed_domains`, `REDIRECT_ENABLED=False`. Known accepted residual: DNS-rebinding/TOCTOU on the Scrapy path (it re-resolves at connect).

## Conventions

- **Type gate is two-tier** (`pyproject [tool.mypy]`): `lcp.core.* / lcp.pipeline / lcp.adapters.*` are held to a strict bundle; only the `cli.py`/`gui.py` shells stay non-strict. The strict flags are **enumerated, not `strict = true`** — a per-module `strict` would apply globally and force strict onto the shells. To tighten a shell, add it to the strict override module list.
- **`no_implicit_reexport` is on** for strict modules — re-exports must be explicit (`from x import y as y`), as in `pipeline.py`'s draft-store re-exports.
- Errors flow through the `core/errors.py` hierarchy (`LcpError` → `InputValidationError`, `ExternalServiceError`, `DependencyError`, …); CLI maps them to exit codes per the error contract.
- Tests mirror the `src/` tree under `tests/` (`tests/core`, `tests/processor`, …). No `conftest.py`, no custom markers; GUI tests `importorskip("webview")`.
- Comments explain **why**, not what (per repo style). Commit messages are English.

## Where to read more

`docs/plans/` (numbered implementation plans, e.g. `2026-06-17-002-feat-content-pipeline-upgrade-plan.md`), `docs/brainstorms/` (requirements), `docs/security/pii-inventory.md`, and `docs/*-runbook.md` (operator go/no-go procedures).
