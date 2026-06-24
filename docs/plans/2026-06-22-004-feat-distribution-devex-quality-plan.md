---
title: "feat: distribution, developer experience & quality boundaries"
type: feat
status: completed
date: 2026-06-22
origin: docs/brainstorms/2026-06-22-lcp-extensibility-robustness-requirements.md
---

# feat: distribution, developer experience & quality boundaries

## Summary

Turn `lcp` from an internal MVP into a reusable open-source tool. The plan covers three pillars that plan-003 intentionally left out: **distribution** (LICENSE, PyPI, CHANGELOG, wheel integrity), **developer experience** (Docker, pre-commit, quickstart, CI gates), and **quality boundaries** (E2E tests, error-handling audit, performance profiling). Work is sequenced by risk gradient: the lowest-risk/highest-value items come first so each phase ships independently without blocking on later phases.

---

## Problem Frame

Plan-003 (engine extensibility + robustness) addresses the internal architecture maturity. But three classes of gap remain before the tool is *reusable by others*:

1. **Nobody can install it.** No LICENSE, no PyPI package, no CHANGELOG, no version tags. `pip install lcp` is a 404. Wheel assets (`web/`) aren't even in the built package.
2. **Nobody can contribute efficiently.** No Docker for a reproducible environment, no pre-commit hooks, no "5-minute quickstart" section, no CI lint/security gates, no benchmark regression detection.
3. **Nobody can trust the boundary.** No real E2E tests (only unit/mock-level integration), no systematic error-handling audit, gossip_scraper lives outside mypy/CI, no performance profile for operator planning.

These gaps are orthogonal to plan-003's gate-registry/injection-container work — they can proceed in parallel, with only README sections potentially touching the same files.

---

## Requirements

- R1. Installable by `pip install lcp` — LICENSE, CHANGELOG, PyPI release workflow, functional wheel.
- R2. Reproducible development environment — Dockerfile, pre-commit hooks, CI lint gates.
- R3. Trustable quality boundary — E2E tests, error-handling audit, gossip_scraper gates, performance baselines.

---

## Scope Boundaries

- **No engine architecture changes** — that's plan-003's domain (gate registry, injection container, shared primitives, batch worklist, config seam fixes).
- **No auto-publish** — that's plan-002's domain.
- **No gossip feature breadth** (Douyin, ranking decoupling) — that's plan-001.
- **No SPA crawler, no de-watermark** — already de-scoped by prior decisions.
- **No formal verification or cryptographic guarantees** — the existing best-effort/plaintext-erasure honesty stance is preserved.

---

## Context & Research

### Relevant Code and Patterns

- **Existing CI:** `.github/workflows/ci.yml` — runs `mypy` then `pytest -q`. Used as base for CI expansion.
- **Packaging:** `pyproject.toml` (setuptools, console-script entry point, optional-dependency groups). The `git-master` patterns for version tags exist in the codebase.
- **Tests:** 903 tests in 50 test files — the E2E tests should mirror the existing `test_e2e_*.py` pattern with `FakeCrawler` + `FakeLlmClient`.
- **Web assets:** `src/lcp/web/` — currently not included in wheel (`[tool.setuptools.package-data]` missing).
- **gossip_scraper:** Lives at project root, outside `[tool.mypy] files` — plan-003's R10 will address this.
- **Error handling:** `core/errors.py` defines `LcpError` hierarchy with 5 exit codes. The audit targets `except` clauses that catch overly broad types.

### Institutional Learnings

- `docs/solutions/mypy-from-venv-not-pyenv.md` — run mypy from `.venv`, not system Python.
- `docs/solutions/unit-tests-mask-integration-bugs.md` + `real-happy-path-unreachable-masked-by-green-tests.md` — E2E tests must exercise the *real* gate chain, not a shortcut.
- `docs/solutions/atomic-write-temp-replace.md` — relevant for understanding existing primitives before adding tooling.

### External References

- [Keep a Changelog](https://keepachangelog.com/) — versioned changelog format.
- [SemVer](https://semver.org/) — version numbering convention.
- [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/) — OIDC-based deploy without API tokens.
- [Docker best practices for Python](https://docs.docker.com/language/python/) — multi-stage builds, slim images.

---

## Key Technical Decisions

- **LICENSE = MIT.** Maximum adoption for a dev-tool/library. Zero friction for downstream users.
- **PyPI deploy via Trusted Publishing (OIDC).** Safer than API tokens, no secret management.
- **Docker base = `python:3.11-slim`.** Matches README requirement, keeps image small. ffmpeg from apt.
- **E2E tests use `FakeCrawler` + `FakeLlmClient` + real gate chain.** Zero network, zero API cost, fully deterministic. Mirrors existing `test_e2e_*.py` pattern.
- **`pre-commit` over custom hooks.** Industry standard, community-maintained, simpler than writing hook scripts.
- **Benchmark CI runs on release branches only, not every PR.** Keeps feedback fast while catching regressions.

---

## Open Questions

### Resolved During Planning

- Should gossip_scraper be moved into `src/lcp/`? **Deferred to plan-001's scope — this plan only brings it under mypy + CI gates.**
- Should PyPI deploy use `__version__` in `lcp/__init__.py` or `importlib.metadata`? **Use `importlib.metadata` — standard library, single source of truth from pyproject.toml.**
- Should we add a `ruff` linter in addition to mypy? **Yes — `ruff check + ruff format` is significantly faster than `black + isort + flake8` and catches more issues.**
- Should the Dockerfile use `pip install lcp[crawl,media,llm,dedup]` or install from source? **Install from source (editable or local wheel) — most contributors will be developing, not just consuming.**

### Deferred to Implementation

- Exact pre-commit hook version pins — resolved at install time by pre-commit's own update mechanism.
- Exact benchmark thresholds — discovered on first run, not pre-declared.

---

## Implementation Units

### U1. Distribution foundation: LICENSE, CHANGELOG, version tags

**Goal:** Establish the legal and documentation foundation for distribution. A new contributor can see the license, the release history, and the current version.

**Requirements:** R1

**Dependencies:** None

**Files:**
- Create: `LICENSE`
- Create: `CHANGELOG.md`
- Modify: `README.md` (add license badge, changelog link)

**Approach:**
- Write MIT license text verbatim.
- Generate initial CHANGELOG from git log (`git log --oneline --reverse v0.1.0..HEAD` if tag exists, otherwise full history).
- Adopt Keep a Changelog format with `[Unreleased]` section for current work + dated releases below.
- Add `[project.urls]` with `"Source"`, `"Issues"`, `"Changelog"` to `pyproject.toml`.

**Test scenarios:**
- Happy path: `pip install -e .` succeeds after changes (no import regression).
- Content check: `LICENSE` exists and contains "MIT License".
- Content check: `CHANGELOG.md` exists, parses as valid markdown with `[Unreleased]` heading.

**Verification:**
- `./.venv/bin/python -m pytest -q` passes.
- `./.venv/bin/mypy` passes.
- LICENSE, CHANGELOG.md created.

---

### U2. PyPI publish pipeline + wheel fix

**Goal:** `pip install lcp` works. `pip install lcp[crawl,media,llm,dedup,gui]` works. CI can publish to PyPI on tag push.

**Requirements:** R1

**Dependencies:** U1 (LICENSE, CHANGELOG — needed before first publish)

**Files:**
- Modify: `pyproject.toml` (add `[project.urls]`, add `[tool.setuptools.package-data] lcp = ["web/*"]`, add classifiers)
- Create: `.github/workflows/release.yml`
- Modify: `README.md` (remove the "install from source" instructions for basic usage, add `pip install lcp`)

**Approach:**
- Add package-data for web assets: `[tool.setuptools.package-data] lcp = ["web/*"]`.
- Add classifiers: `"Development Status :: 4 - Beta"`, `"License :: OSI Approved :: MIT License"`, `"Programming Language :: Python :: 3.11"`, etc.
- Release workflow triggers on `v*` tag push:
  1. Build wheel + sdist via `pypa/build`
  2. Verify wheel contents include `lcp/web/` assets
  3. Publish to PyPI via `pypa/gh-action-pypi-publish` with `trusted-publishing: true`
  4. Create GitHub Release with CHANGELOG content for this version
- Add `[tool.setuptools.package-data] lcp = ["web/*"]` so `lcp gui` works from a non-editable install.

**Test scenarios:**
- Happy path: `python -m build` produces a wheel; `unzip -l dist/*.whl | grep web/` shows `lcp/web/index.html`, `app.js`, `app.css`, `lex.js`.
- Integration: `pip install dist/*.whl` in a fresh venv → `lcp gui` starts without ImportError.

**Verification:**
- `python -m build` succeeds and wheel contains web assets.
- `./.venv/bin/python -m pytest -q` still passes (no functional change).
- CI workflow is syntactically valid YAML.

---

### U3. pre-commit hooks

**Goal:** Every commit is automatically linted and type-checked. Consistent code style across all contributors.

**Requirements:** R2

**Dependencies:** None

**Files:**
- Create: `.pre-commit-config.yaml`
- Modify: `pyproject.toml` (add `ruff` to dev dependencies and tool config)
- Modify: `README.md` (add pre-commit install step to setup)

**Approach:**
- `.pre-commit-config.yaml` with:
  - `ruff format` + `ruff check --fix` — fast Python lint + format
  - `mypy` (from the project venv, via `system` hook)
  - `check-toml`, `check-yaml`, `trailing-whitespace`, `end-of-file-fixer`
  - `mixed-line-ending` (LF)
- `ruff` as optional dependency in `[project.optional-dependencies] dev`.
- `[tool.ruff]` section in `pyproject.toml` (minimal: `line-length = 100`, `target-version = "py311"`, select `["E", "F", "I", "W"]`).
- Install step in README: `pre-commit install` after cloning.

**Patterns to follow:**
- Existing `pyproject.toml` dev deps section.

**Test scenarios:**
- Happy path: `pre-commit run --all-files` exits 0.
- Content check: `.pre-commit-config.yaml` is valid YAML with expected repo entries.
- Integration: a deliberate ruff violation is caught by `pre-commit run`.

**Verification:**
- `pre-commit run --all-files` succeeds.
- `./.venv/bin/python -m pytest -q` passes (no behavioral change).

---

### U4. Dockerfile + docker-compose

**Goal:** One command to get a running lcp environment. New contributors and operators don't need to install Python/ffmpeg by hand.

**Requirements:** R2

**Dependencies:** None

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Modify: `.dockerignore`
- Modify: `README.md` (add Docker quickstart section)

**Approach:**
- Multi-stage `Dockerfile`:
  - **Build stage:** `FROM python:3.11-slim`, install build deps, copy source, `pip install .[crawl,media,llm,dedup,gui,dev]`.
  - **Runtime stage:** `FROM python:3.11-slim`, install runtime deps (`ffmpeg` from apt), copy wheel from build stage, entrypoint = `lcp`.
- `docker-compose.yml`:
  - Mounts `./data` → `/data` (persistent job storage)
  - Mounts `./config.yaml` → `/app/config.yaml`
  - Exposes port `8765` for GUI
  - `command: lcp gui` by default
- `.dockerignore` excludes `.venv/`, `.git/`, `__pycache__/`, `data/`.

**Test scenarios:**
- Happy path: `docker compose build` exits 0.
- Integration: `docker compose run --rm lcp init` creates config.yaml skeleton.
- Integration: `docker compose run --rm lcp --help` shows command list.

**Verification:**
- `docker compose build` succeeds.
- `docker compose run --rm lcp init` produces output.

---

### U5. CI expansion: lint + security + coverage gates

**Goal:** CI catches formatting issues, security vulnerabilities, and coverage regressions automatically.

**Requirements:** R2, R3

**Dependencies:** U3 (ruff config to use in CI)

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `pyproject.toml` (add coverage config if needed)

**Approach:**
- Expand `.github/workflows/ci.yml` into a matrix or sequential pipeline:
  1. **lint:** `ruff check .` + `ruff format --check .`
  2. **type:** `mypy` (existing)
  3. **test:** `pytest -q --tb=short` (existing)
  4. **security:** `pip-audit` on the installed package (optional, small surface)
- Keep the existing `mypy` + `pytest` parallel execution for fast feedback; add lint as a third parallel job.
- No coverage threshold gate yet — collect coverage for visibility but don't fail CI on it.

**Test scenarios:**
- CI dry-run: `.github/workflows/ci.yml` is valid YAML with expected jobs.
- Integration: `ruff check . --help` confirms ruff is installed.

**Verification:**
- `.github/workflows/ci.yml` passes `act --dry-run` or `yamllint`.
- `./.venv/bin/python -m pytest -q` still passes.

---

### U6. E2E test suite

**Goal:** Real gate-chain execution from ingest to REVIEW_PENDING, using fakes for network/LLM. Catch regressions in the full pipeline that unit tests miss.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/test_happy_path_dedup.py`
- Create: `tests/e2e/test_risk_redline.py`
- Create: `tests/e2e/test_full_flow_signoff.py`

**Approach:**
- Three E2E tests, each driving the real `Pipeline` with `FakeCrawler` + `FakeLlmClient`:
  1. **`test_happy_path_dedup`**: Ingest two identical source folders → first one reaches `PROCESSED` (or passes gates), second one is caught as `DUPLICATE` by dedup gate.
  2. **`test_risk_redline`**: Ingest a source folder containing redline content → risk gate parks at `BLOCKED`.
  3. **`test_full_flow_signoff`**: Full loop: ingest → process → review-packet → approve → backfill. Verifies every state transition, the freeze hash binding, and the audit log shape.
- Each test uses real `JobStore` (temp dir), real `Manifest` I/O, real `AuditLog`, real gate checkers — only the crawler and LLM client are fakes.
- Files go in `tests/e2e/` (new subdirectory) to distinguish from unit/integration tests.

**Patterns to follow:**
- Existing `test_e2e_pipeline.py` — uses `Pipeline` + `FakeCrawler` + `FakeLlmClient`.
- Existing `test_e2e_fail_closed.py` — verify that fail-closed gates work.

**Test scenarios:**
- Happy path: duplicate ingest → `DUPLICATE` state.
- Happy path: redline content → `BLOCKED` with correct `RiskCategory`.
- Happy path: full signoff loop → `PUBLISHED_RECORDED`, audit has `EVENT_APPROVED` + `EVENT_BACKFILL`.
- Edge case: frozen packet body tampering detected at approve time (hash mismatch).
- Edge case: signing off without a review packet returns an error, not a crash.

**Verification:**
- `./.venv/bin/python -m pytest tests/e2e/ -q` passes.
- Real `JobStore` + `Manifest` files are created on disk (visible in temp dir).

---

### U7. Error-handling boundary audit

**Goal:** Every `except` clause in the critical path is justified. No silent swallowing, no overly-broad catches that could mask bugs.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Create: `tests/audit/test_except_boundaries.py`
- Modify: Targeted files identified by the audit

**Approach:**
- Use `ast-grep` to find ALL `except` clauses in `src/lcp/`:
  ```python
  # Search pattern: all except clauses
  except $E:
  ```
- Classify each into one of:
  - ✅ **Justified** — catches a specific known exception type (e.g., `except FileNotFoundError`, `except ExternalServiceError`)
  - ✅ **Justified** — broad but immediately re-raises or transforms (e.g., `except Exception: raise LcpError(...)` in a boundary)
  - ⚠️ **Needs tightening** — broad `except Exception` in sensitive pathways
- For ⚠️ findings: narrow to the specific exception type or add a comment explaining why broad is necessary.
- Write a characterization test that documents the expected exception types at key boundaries.

**Specific attention areas:**
- `webserver.py` request handlers — must not leak internal error details.
- `pipeline.py` stage entry/exit — known `ExternalServiceError` retry path; verify no other broad catch.
- `cli.py` command wrappers — should map known `LcpError` types to exit codes.
- `crawl_runner.py` subprocess calls — `ExternalServiceError` expected; verify no silent pass.

**Patterns to follow:**
- `core/errors.py` — the `LcpError` hierarchy and its explicit exit-code contract.

**Test scenarios:**
- Characterization: Each known boundary maps exceptions to the correct `LcpError` subtype.
- Regression: No `except Exception` is introduced without corresponding `LcpError` mapping.

**Verification:**
- Audit report lists all `except` clauses and their classification.
- Any changes pass `./.venv/bin/python -m pytest -q` and `./.venv/bin/mypy`.

---

### U8. Docker quickstart in README + 5-minute tutorial

**Goal:** A new user can go from zero to a running lcp pipeline in 5 minutes without reading architectural documentation.

**Requirements:** R2

**Dependencies:** U4 (Dockerfile must exist)

**Files:**
- Modify: `README.md`

**Approach:**
- Add a `## Quickstart (5 minutes)` section at the top of README (above the existing detailed sections):
  ```
  ## Quickstart (5 minutes)

  ```sh
  # 1. Start lcp
  docker compose up -d
  docker compose exec lcp lcp init

  # 2. Ingest sample content
  docker compose exec lcp lcp ingest --job-id demo-001 --dir ./samples/demo-001

  # 3. Process
  docker compose exec lcp lcp process --job-id demo-001 --title "A sample title of 25-35 chars" --dry-run

  # 4. Open the GUI
  open http://127.0.0.1:8765
  ```
  ```
- Create `samples/demo-001/` with a minimal test article (markdown text + one image).

**Patterns to follow:**
- Existing README structure (starts with overview, follows with install/setup, then quickstart CLI).

**Test scenarios:**
- README renders correctly on GitHub (no broken markdown).
- Each command in the quickstart is runnable (`docker compose exec lcp lcp --help` at minimum).

**Verification:**
- `./.venv/bin/python -m pytest -q` passes (no code changes).

---

### U9. Benchmark CI + performance profile

**Goal:** Detect performance regressions before they ship. Give operators a sense of expected resource usage.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Create: `spikes/benchmark/run.py`
- Create: `spikes/benchmark/README.md`
- Modify: `.github/workflows/release.yml` (add benchmark step on release branches)

**Approach:**
- Benchmark script measures:
  - `lcp list` on 0 jobs → startup latency
  - `lcp list` on 100 synthetic jobs → list latency
  - Process a small job (10KB text) → end-to-end processor throughput
  - Process a large job (500KB text, mocked LLM) → memory estimate via `tracemalloc`
- Outputs JSON: `{"job_count": 0, "elapsed_ms": 42, "peek_mb": 15.3}`.
- CI runs benchmark on release branch pushes, compares against the last tagged release, prints warning if >20% regression.

**Patterns to follow:**
- Existing `spikes/detection_accuracy/run_eval.py` pattern for spike structure.

**Test scenarios:**
- Happy path: `./.venv/bin/python spikes/benchmark/run.py` exits 0 and outputs valid JSON.
- Edge case: 0 jobs → nonzero elapsed_ms (measurement overhead is non-zero).
- Content check: JSON output has expected keys (`job_count`, `elapsed_ms`, `peek_mb`).

**Verification:**
- `./.venv/bin/python spikes/benchmark/run.py --help` shows usage.
- `./.venv/bin/python -m pytest -q` passes (no production code changes).

---

## System-Wide Impact

- **Interaction graph:** U2 (release workflow) and U5 (CI expansion) modify `.github/workflows/` — the only shared infrastructure surface.
- **Error propagation:** U7 (error audit) is purely analytical — no new error paths, only possible tightening of existing ones.
- **State lifecycle risks:** None. U6 (E2E tests) uses temp directories; Docker (U4) uses bind mounts; none affect existing state.
- **API surface parity:** README sections (U8) may be affected by U4 and U2 changes; keep in sync.
- **Integration coverage:** U6 fills the gap between unit tests and manual operator testing — the real gate chain with fake I/O.
- **Unchanged invariants:** No changes to `core/state.py`, `pipeline.py` adapter wiring, `adapters/` business logic, or `cli.py` command signatures. The plan adds files and modifies only `pyproject.toml`, `README.md`, `.github/`, and non-production profiles.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| **Docker image too large** | Use `python:3.11-slim` + multi-stage build to strip build deps from runtime image. |
| **PyPI release workflow breaks** | Test with `--repository testpypi` first, then flip to production. Trusted Publishing removes API-token management risk. |
| **E2E tests flaky from filesystem timing** | Use `tempfile.mkdtemp()` + sync writes. The existing 903 tests already use this pattern successfully. |
| **pre-commit adds friction to simple commits** | Keep the hook list minimal: ruff + mypy + basic file checks. No slow or opinionated hooks. |
| **Benchmark CI adds minutes to release workflow** | Run only on release branches (not every PR), with 60s timeout. |

---

## Phased Delivery

### Phase 1 — Distribution (U1, U2)
Lowest risk, highest impact. Ship LICENSE, CHANGELOG, and PyPI workflow so `pip install lcp` works. The wheel fix is one line.

### Phase 2 — DevEx foundation (U3, U4, U5, U8)
Pre-commit hooks + Docker + CI gates + quickstart tutorial. These are additive (no behavioral changes) and make the project immediately more contributor-friendly.

### Phase 3 — Quality boundaries (U6, U7)
E2E tests + error-handling audit. These touch only test files and provide the confidence foundation for future contributors to make changes safely.

### Phase 4 — Performance (U9)
Benchmark CI + performance profile. Lowest priority — most useful after the tool has external users reporting slowdowns.

---

## Documentation / Operational Notes

- **README** changes: add license badge, Docker quickstart, pre-commit install step. Existing detailed sections (architecture, install, security) remain.
- **CHANGELOG.md** becomes the single source of truth for release notes. Git tags trigger the PyPI workflow and GitHub Release.
- **No monitoring or PagerDuty** — this is a local CLI tool, not a service.
- **No feature flags** — all changes are additive and gated only by CI.

---

## Sources & References

- **Origin document (partial):** Conversation brainstorm output (2026-06-22) — A+B+C distribution/DevEx/quality requirements.
- **Related plan:** `docs/plans/2026-06-22-003-refactor-engine-extensibility-robustness-plan.md` — engine internals maturity (this plan's sibling, not dependency).
- **Related code:** `.github/workflows/ci.yml`, `pyproject.toml`, `src/lcp/web/`.
- **Existing pattern:** `spikes/detection_accuracy/run_eval.py` for benchmark spike structure.
- **Existing pattern:** `test_e2e_pipeline.py` for E2E test approach.
