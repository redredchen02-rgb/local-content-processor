# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`gossip` optional extras group**: `gossip = ["httpx>=0.27,<1"]` declared in
  `pyproject.toml`; CI installs it in all three jobs so `httpx` is available for
  mypy type-checking
- MIT License
- PyPI release workflow (Trusted Publishing via OIDC)
- `[project.urls]`, classifiers, and package-data for web assets
- **CI security gate**: `pip-audit` job scans the resolved dependency closure
  (exit code is the gate); advisory on the release path
- **CI coverage gate**: `pytest-cov` reporting with a low `--cov-fail-under`
  soft floor (regression insurance, ratchets upward)
- **Runnable quickstart**: `samples/demo-001/` text bundle drives the real
  pipeline to `REVIEW_PENDING`; dedicated real-decodable-image media-gate test
- **Release version-sync gate**: `scripts/check_tag_matches_version.py` fails a
  tagged build loud when the pushed tag ≠ `pyproject` version (type-checked + tested)

### Changed

- **`gossip_scraper` under mypy + ruff gates**: `extend-exclude` lifted from ruff,
  `gossip_scraper` added to `[tool.mypy] files`; 10 mypy errors resolved (dict type
  annotations, `max()` lambda key functions, `ScraperProtocol` parameter/variable
  types); ruff F401/I001 auto-fixed; httpx `ignore_missing_imports` override added
- **`__version__` single source**: sourced from installed package metadata
  (`importlib.metadata`), eliminating the `pyproject`↔`__init__.py` drift
- **Release workflow hardened** to a fail-closed, concurrency-serialized
  three-job chain (`build → publish-pypi → github-release`): per-job least
  privilege (`id-token` only on publish, `contents:write` only on release),
  tag-ancestor + version-sync + CHANGELOG-section pre-publish gates, `twine
  check --strict`, fresh-venv install smoke, cross-job dist digest re-verify,
  all actions SHA-pinned, benchmark removed from the release path

## 0.1.0 — 2026-06-18

### Added

- **Scaffold**: project skeleton, `pyproject.toml`, CLI entry point via Click,
  OS keyring integration for LLM API key, OS hardening (umask `0o077`, subprocess
  PATH pinning)
- **Storage**: `JobStore`, `AuditLog`, `Manifest` — PII-free SQLite index,
  append-only JSONL audit, sha256 manifest hashing, idempotent job state
- **State machine**: pure transition table (`core/state.py`) — happy path
  `NEW → PUBLISHED_RECORDED`, fail-closed side branches (`BLOCKED`,
  `DUPLICATE`, `NEEDS_HUMAN_REVIEW`), transient `PROCESSING` with
  marker-file crash recovery
- **Crawler**: Scrapy subprocess crawl with scrubbed environment, local folder
  ingest, SSRF/path-traversal guards (DNS `is_global`, scheme allowlist,
  `REDIRECT_ENABLED=False`)
- **Media processing**: Pillow/ffprobe asset validation and normalization
  (800px default, 1300×640 cover, bomb guards, timeout)
- **Risk gate**: hard-stop redline detection (defamation/privacy keywords) —
  fail-closed to `BLOCKED`
- **Dedup gate**: advisory cascade using `datasketch` MinHash + `site_index.jsonl`
  — fail-loud to `NEEDS_HUMAN_REVIEW`
- **LLM client**: OpenAI-compatible Chat Completions with TLS pinning (R40),
  config-driven `ca_bundle`/`allow_http_hosts` escape hatch
- **Assembler**: constrained-rewrite draft generation from source material
- **Copywriter**: AI-generated `quick_facts`, `summary`, `faq` sections
- **Linter + Grounding**: CJK-aware substring grounder with opt-in +NLI LLM
  entailment judge
- **Sanitizer**: output-side HTML escaping (R41), source URLs as inert text
- **Review packet**: body-hash freeze binding, sign-off attribution, backfill
  attestation loop
- **GUI (pywebview)**: minimal `js_api` shell with `textContent` render, CSP,
  CLI/GUI parity
- **GUI (webui)**: loopback `http.server` replacement — fail-closed auth chain
  (Host + per-launch token + Origin/Sec-Fetch-Site), `POST /api/*` JSON
  endpoints, fetch-proxy bridge, security headers
- **CLI discovery**: auto-load `./config.yaml` from cwd (exists-gated),
  `--config` override
- **Config**: `config.yaml` with domain allowlists, reviewer whitelist,
  LLM endpoint config; `pydantic-settings` schema with built-in defaults
- **Runtime hardening**: PATH pinning, `chmod 0600` on `lcp.db`, `fsync` on
  audit directory, `fcntl` append lock, `umask 0o077` at startup
- **Error contract**: `LcpError` hierarchy with explicit exit codes per error
  type
- **Mypy type gate**: two-tier config (`core.*` + `pipeline` + `adapters.*`
  strict, shells non-strict)

### Fixed

- `crawler`: malformed media URLs dropped instead of aborting entire extraction
- `dedup`: malformed site index handled as fail-closed, not crash
- `signoff`: frozen title + cover hash re-verified on approve; `draft.json`
  absence fails loud
- `media`: untrusted ffprobe numerics handled closed
- `crawler`: subprocess failures surfaced as retriable
- `rules`: false-terminal `BLOCKED`/`DUPLICATE` from substring/empty-title
- `crash-recovery`: `.processing` marker actually consumed by `reconcile()`
- `storage`: atomic Stage-1 write (state + hashes); `delete_job` with
  `BEGIN IMMEDIATE`
- `crawler`: bounded DNS resolution timeout in SSRF preflight
- `crawler`: cleaned `raw/` on failed crawl; contained pipeline-output path
- `media`: bounded CPU on Pillow cover-analysis path
- `LLM`: per-process cooldown after repeated provider failures
- `gui`: uniform bridge safety on `cover_report` + `disclaimer`
- `gui`: Web Inspector gated behind `LCP_GUI_DEBUG` env var
- `signoff`: no false `SIGNOFF_INVALIDATED` when superseding never-signed-off holds
- `net_guard`: serialized DNS-timeout bound under a lock
- `pipeline`: allow in-place re-crawl of a `CRAWL_FAILED` job
- `reconcile`: pure read; bump crash counter on retry, not on views

### Changed

- **Refactor batch 1**: robustness and atomicity quick wins
- **Refactor batch 2**: SQLite connection churn reduced, `O(n²)` audit append
  eliminated
- **Refactor batch 3**: crawler seam unified, Stage-1 triplication removed
- **Refactor batch 4**: layering fixed (`core` → `adapters`, `publisher` →
  orchestrator)
- **Refactor batch 5**: shingle memo for grounding, honest Stage-2 docstrings
- **Refactor batch 6**: `core/config.py` split into pure schema + I/O adapter
- **Refactor batch 7**: `build_crawler` + `now` helpers extracted, shell
  duplication eliminated
- **Refactor batch 8**: pure extraction policy moved to `core/rules`
- **Refactor batch 9**: signoff `resolve` lint verdict routed through processor
- **Refactor batch 10**: `@bridge_safe` decorator, typed dedup params
- **De-watermark pipeline**: entire Batch 2 (watermark removal) cut — the
  pipeline has no watermark-removal path
- **CI**: mypy type-check step added as blocking gate

### Removed

- Pywebview dependency: `gui` extra is now empty; GUI is a stdlib `http.server`
  webui
- De-watermark engine: entire Batch 2 CUT
- `lcp.gui` no longer launches a desktop window; `lcp gui` launches the webui

## 0.0.1 — 2026-06-01

### Added

- Initial project scaffold
