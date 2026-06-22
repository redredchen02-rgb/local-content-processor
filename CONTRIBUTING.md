# Contributing to lcp

Thank you for considering contributing to `local-content-processor`.

## Architecture at a Glance

```
src/lcp/
  core/          Pure functional core (NO I/O, NO framework)
  adapters/      Imperative shell (I/O, subprocess, network)
  pipeline.py    Injection seam — holds adapters, runs stages, drives state machine
  cli.py         Thin CLI shell (Click)
  gui.py         Thin GUI bridge (Api class, mirrors CLI 1:1)
  webserver.py   stdlib http.server — serves web/ + exposes Api as /api/* JSON
  web/           Frontend assets (index.html, app.js, lex.js, app.css)
```

## The Golden Rule: Functional Core / Imperative Shell

All **business judgement** lives in `core/` (models, rules, state machine). All **I/O** lives in `adapters/`. `pipeline.py` is the injection seam that wires them together. CLI/GUI are thin shells that parse args and call the pipeline.

When adding a feature:
1. **Decision** → `core/` (pure, testable, no side effects)
2. **I/O** → `adapters/` module (one adapter per concern)
3. **Wire** → `pipeline.py` (inject the adapter, call the core)
4. **Expose** → **both** `cli.py` AND `gui.py` (the 1:1 mirror rule)

## Adding a New Gate (Stage 2)

The gate chain is **declarative** (`adapters/processor/gate_registry.py`):

```python
# In gate_registry.py:
def _run_my_new_gate(ctx: GateContext) -> JobState | None:
    # Your gate logic here
    return None  # pass, or JobState.XXX to park

PARK_GATES = [
    GateSpec("risk", _run_risk_gate),
    GateSpec("media", _run_media_gate),
    GateSpec("dedup", _run_dedup_gate),
    GateSpec("my_new_gate", _run_my_new_gate),  # ← add here
]
```

Order matters: fail-closed, cheap-first. The runner stops at the first gate that returns a `JobState`.

## Adding a New Adapter

1. Add a field to `Adapters` in `adapters/container.py`:
   ```python
   @dataclass(frozen=True)
   class Adapters:
       store: JobStore
       audit: AuditLog
       llm_client: LlmClient
       crawler: CrawlerProtocol | None = None
       my_new_adapter: MyNewAdapter | None = None  # ← add here
   ```
2. Wire it in `Pipeline.__init__` (or use it directly in the relevant stage).
3. Add tests in `tests/`.

## Adding a New Operator Action

Both shells must mirror each other:

1. **CLI**: Add a `@cli.command()` in `cli.py`
2. **GUI**: Add a method to `Api` in `gui.py` (same name, same semantics)
3. **Verify**: Run `pytest tests/test_cli_gui_parity.py` — it catches one-sided additions

## The Persist Seam (Stage 2 → SQLite)

Stage-2 gates **cannot** persist `PROCESSING → target` directly because `PROCESSING` is transient. Use:

```python
from lcp.adapters.processor._persist import persist_gate_state

persist_gate_state(store, job_id, JobState.BLOCKED, updated_at=ts)
```

This validates the canonical `persisted_current → PROCESSING → target` edge, persists the resting state, and clears the `.processing` marker.

## Running Tests

```sh
./.venv/bin/python -m pytest -q          # full suite (~1043 tests)
./.venv/bin/mypy                         # type gate
pre-commit run --all-files               # lint + format
```

CI runs `ruff check` + `mypy` + `pytest -q`. All three must be green.

## Code Style

- **No comments** unless the WHY is non-obvious
- **English** commit messages
- **Minimal abstractions** — three similar lines beats a premature abstraction
- Type gate: `core/*`, `pipeline`, `adapters/*` are strict (`mypy --strict` subset); shells (`cli.py`, `gui.py`) are non-strict

## Security Invariants (Never Break These)

- **No publish without a human.** `approve` is attribution, not publication.
- **PII-free audit.** Only hashes + enum codes in `audit.jsonl`.
- **LLM is zero-capability.** One Chat Completions call, no tools, no writes.
- **SSRF guards.** Scheme allowlist, DNS `is_global`, `REDIRECT_ENABLED=False`.
- **0600 at rest.** `apply_hardening()` sets umask `0o077` at startup.
