---
title: "fix: CLI auto-discovers config.yaml from cwd (lcp run honours lcp init)"
type: fix
status: active
date: 2026-06-18
deepened: 2026-06-18
---

# fix: CLI auto-discovers config.yaml from cwd

## Overview

`lcp init` scaffolds `config.yaml` in the current directory and tells the operator
*"Next: edit config.yaml (add a reviewer), then `lcp run`."* But every operator
command builds its context via `Ctx(ctx.obj)` → `load_config(obj.get("config_path"))`,
and when no `--config` was passed `config_path` is `None`, so `load_config(None)`
returns **defaults** — it never reads the `config.yaml` that `init` just wrote.

Result: an operator who follows the `init` instructions runs `lcp run`/`process`/
`approve` against an **empty config** (no reviewer whitelist → every sign-off
blocked; empty `allow_domains` → every crawl rejected at preflight), with no signal
that their `config.yaml` was ignored. `init`'s own promise is false.

This is the **CLI twin** of the GUI bug fixed in PR #12 (the Settings panel wrote
`config.yaml` but read defaults). The fix is the same shape: resolve the config
path so the tool **reads from where `init` writes**. The CLI fix lands at the
single `Ctx` chokepoint.

## Problem Frame

- **Who is affected:** the non-technical operator (and anyone) running the
  documented `lcp init` → edit → `lcp run` flow without `--config`.
- **Why it surfaced now:** the GUI fix (PR #12) exposed the shared root pattern —
  `load_config(None)` returns defaults; the GUI fix resolved its path, the CLI was
  left on the same footgun (operator decision 2026-06-18: fix the CLI in a separate
  PR). See [[project-gui-localhost-webui]].
- **Why tests didn't catch it:** the suite drives commands with an explicit
  `--config`/`--output-dir` or fixtures, so the real "no `--config`, rely on cwd
  `config.yaml`" path is never exercised — the `unit-tests-mask-integration-bugs`
  / `real-happy-path-unreachable-masked-by-green-tests` pattern.

## Requirements Trace

- **R1.** When no `--config` is given and a `config.yaml` exists in the current
  working directory, `Ctx` loads it (so `lcp init` → `lcp run` honours the file).
- **R2.** When no `--config` is given and **no** `config.yaml` exists in cwd,
  behavior is unchanged: defaults are loaded (CI / fresh-dir safe — no error).
- **R3.** An explicit `--config PATH` is unchanged: it loads `PATH`, and a missing
  `PATH` still raises the typed `InputValidationError` (exit 2) — the new implicit
  default must not silently mask a bad explicit path.
- **R4.** No regression to the existing suite or the type gate; a new test drives
  the real `init → command` path that the bug lived in.

## Scope Boundaries

- **Non-goal:** ancestor-directory search / config discovery walking up the tree
  (git-style). Scope is **cwd only**, matching where `init` writes.
- **Non-goal:** changing `load_config` semantics, the `Config` model, or any
  compliance gate (reviewers/allow_domains logic). This only changes *which file*
  the CLI reads by default.
- **Non-goal:** touching the GUI/`webserver` path — PR #12 already resolved it via
  `webserver._make_api`. (This plan notes the intentional GUI/CLI asymmetry but does
  not unify them; see Key Technical Decisions.)
- **Non-goal:** changing `--output-dir`/`base_dir` resolution (it already falls back
  to `config.storage.base_dir`, which now benefits automatically once the config is
  read).

## Context & Research

### Relevant Code and Patterns

- `src/lcp/cli.py` — `Ctx.__init__` (the single chokepoint: `self.config =
  load_config(obj.get("config_path"))`, built by all 11 operator commands). `init`
  already uses the cwd convention (`Path(obj.get("config_path") or "config.yaml")`)
  and emits the "then `lcp run`" promise this fix makes true.
- `src/lcp/adapters/storage/config_io.py` — `load_config(path)`: `path is None ->
  Config()` defaults; a given-but-missing path raises `InputValidationError`. This
  is *why* the default must be **exists-gated** (a bare `or "config.yaml"` would
  raise on a fresh dir).
- `src/lcp/webserver.py` — `_make_api(config_path or "config.yaml")` (PR #12): the
  direct prior art for "read where the tool writes." Note the asymmetry below.
- `tests/test_cli_skeleton.py` — CLI invocation pattern (`from lcp.cli import main`,
  `main([...])`, exit-code assertions); the home for the new regression test.

### Institutional Learnings

- `docs/solutions/unit-tests-mask-integration-bugs.md` + `…/real-happy-path-
  unreachable-masked-by-green-tests.md` — the bug is masked because tests pass
  explicit config. The new test **must** drive the real no-`--config` + cwd-
  `config.yaml` path, not a fixture shortcut.
- `docs/solutions/mypy-from-venv-not-pyenv.md` — verify the gate with `./.venv/bin/mypy`.

## Key Technical Decisions

- **Resolve at the `Ctx` chokepoint, exists-gated.** When `config_path` is falsy
  and `Path("config.yaml").exists()`, load `config.yaml`; otherwise pass the
  original value to `load_config` (so `None` → defaults, explicit path → that path).
  *Rationale:* `Ctx` is the one place every command resolves config; exists-gating is
  load-bearing — a naive `obj.get("config_path") or "config.yaml"` (what `init` uses,
  because `init` *creates* the file) would make `load_config("config.yaml")` **raise
  "config file not found"** on every no-`--config` command in a fresh/CI dir. That is
  the catastrophic version of this fix and must be avoided.
- **cwd-relative, not an ancestor walk.** `init` writes to cwd; reading from cwd is
  the symmetric, predictable choice and matches how workspace CLIs (git, npm) treat
  the working directory. Ancestor search is deferred scope.
- **Keep the GUI/CLI resolution asymmetric (do not force a shared helper).** The GUI
  sets `config_path="config.yaml"` *unconditionally* (its Settings panel creates the
  file, so non-existence is fine — the GUI's own internal `gui._Ctx`, distinct from
  this CLI `Ctx`, then existence-checks before loading). The CLI must be exists-gated
  (it never creates the file mid-command and must not error). The two needs differ,
  so a single shared helper would obscure more than it saves; a brief comment
  cross-referencing PR #12 is enough.
- **`init`'s message needs no change** — it becomes *true* once this lands. A short
  doc note that the CLI auto-loads cwd `config.yaml` is the only doc change.

## Open Questions

### Resolved During Planning

- *Auto-discover from cwd, or just fix the `init` message to say `--config`?* →
  Auto-discover (operator chose "fix it"); it makes `init`'s existing promise true
  and mirrors the GUI fix.
- *Default unconditionally vs exists-gated?* → Exists-gated (a bare default would
  break fresh-dir/CI commands by raising on a missing `config.yaml`).
- *Search ancestors?* → No; cwd only (matches `init`).

### Resolved During Planning (continued — review audit)

- *Which existing no-`--config` tests build `Ctx` from the repo cwd and could pick
  up a stray local `config.yaml`?* → **Two**, both in `tests/test_cli_skeleton.py`:
  `test_crawl_off_allowlist_is_input_error` and
  `test_review_packet_without_draft_is_usage_error` (they pass `--output-dir` but no
  `--config` and run from the repo cwd). A repo-root `config.yaml` exists right now
  (gitignored, `allow_domains: ['51cg1.com']`). After the fix it auto-loads;
  `test_crawl_off_allowlist` then still exits 2 only **by accident** (`51cg1.com`
  ≠ `example.com`) rather than for the intended reason (empty default allowlist) — a
  silent trap if a dev later adds `example.com` locally. Unit 1 pins both with
  `monkeypatch.chdir(tmp_path)` (or an explicit empty/tmp `--config`) so they stay
  independent of the developer's local `config.yaml`. CI is unaffected (clean
  checkout has no `config.yaml`).

### Deferred to Implementation

- *Exact placement of the resolution* (inline in `Ctx.__init__` vs a tiny private
  helper in `cli.py`/`config_io.py`) — decide while editing; both satisfy R1–R3. A
  helper is only worth it if a second caller appears.

## Implementation Units

- [ ] **Unit 1: `Ctx` auto-loads cwd `config.yaml` when no `--config` (exists-gated)**

**Goal:** Make every operator command read the `config.yaml` that `lcp init` writes,
so the documented `init → edit → run` flow works without `--config`, while leaving
explicit-path and no-file behavior unchanged.

**Requirements:** R1, R2, R3, R4

**Dependencies:** None

**Files:**
- Modify: `src/lcp/cli.py` (`Ctx.__init__` config-path resolution; brief comment
  cross-referencing the exists-gate rationale + PR #12 GUI twin)
- Test: `tests/test_cli_skeleton.py` (or a new `tests/test_cli_config_discovery.py`)

**Approach:**
- In `Ctx.__init__`, before calling `load_config`, compute the path to load:
  **only** when `config_path` is falsy **and** a cwd `config.yaml` exists, substitute
  `"config.yaml"`; in every other case pass the original `config_path` through
  unchanged. Then `load_config(...)` as today. The existence check is the whole
  point: `None` + no file → `None` → defaults (unchanged, no raise); explicit missing
  `--config` → still passed through → still raises exit 2.
- **Also pin the two affected existing tests** (Resolved/audit above):
  `test_crawl_off_allowlist_is_input_error` and
  `test_review_packet_without_draft_is_usage_error` get a `monkeypatch.chdir(tmp_path)`
  (or explicit empty `--config`) so they no longer depend on the developer's local
  repo-root `config.yaml`, and still assert exit 2 / `EXIT_USAGE` for the intended
  reason.

**Execution note:** Test-first — start with the failing integration test that drives
the real `init → no-`--config` command` path (the seam the bug lived in), then make
it pass. This is the coverage the existing suite lacks.

**Patterns to follow:**
- `init`'s cwd convention (`Path(obj.get("config_path") or "config.yaml")`) and the
  PR #12 GUI fix (`webserver._make_api`), adapted to be exists-gated.

**Test scenarios:**
- *Integration (the regression):* in a clean tmp cwd (`monkeypatch.chdir(tmp_path)`),
  write a `config.yaml` with a non-default marker (e.g. `publisher.reviewers: [alice]`
  and a non-default `storage.base_dir`); build `Ctx({})` (no `config_path`) → its
  `config.publisher.reviewers == ["alice"]` (read from the file, not defaults).
  Stronger variant: run the real `init`-then-command flow via `main([...])` in the
  chdir'd tmp and assert the command sees the configured reviewer/allowlist.
- *Edge — no file:* clean tmp cwd, no `config.yaml`, build `Ctx({})` → config equals
  defaults (R2; proves CI/fresh-dir safety, no raise).
- *Happy — explicit path honoured:* `Ctx({"config_path": str(tmp/"custom.yaml")})`
  with that file present → loads it, not cwd `config.yaml` (explicit wins).
- *Error path — explicit missing path still raises:* `Ctx({"config_path":
  "/nope/missing.yaml"})` → `InputValidationError` (exit 2); the new implicit default
  must NOT swallow this into defaults (R3).
- *Edge — explicit beats cwd:* with BOTH a cwd `config.yaml` and an explicit
  `--config other.yaml`, the explicit one loads.

**Verification:**
- The real `init → run` path (no `--config`) loads the operator's `config.yaml`;
  fresh-dir/no-file still yields defaults with no error; explicit-missing still
  exits 2. `./.venv/bin/mypy` clean; full `pytest -q` green **run from a clean cwd**.

---

- [ ] **Unit 2: Document the cwd config auto-load**

**Goal:** State the (now-true) behavior so operators and future readers know the CLI
reads `config.yaml` from cwd.

**Requirements:** R1

**Dependencies:** Unit 1

**Files:**
- Modify: `README.md` (the configuration/usage section — note `lcp` auto-loads
  `./config.yaml`; `--config` overrides)
- Modify: `CLAUDE.md` (the Commands/config note — CLI reads cwd `config.yaml` by
  default, mirroring `init`'s write location and the GUI)

**Approach:**
- One or two lines each; no behavior described that Unit 1 doesn't implement.
- Include a one-line operator caution (trust-by-location, see Risks): run `lcp` from
  your own/known working directory, or pass `--config` explicitly when unsure — a
  `config.yaml` present in the cwd is loaded as-is (its `allow_domains`/`reviewers`
  take effect). Frame as guidance, not alarm; defaults are fail-safe.

**Test scenarios:** Test expectation: none — documentation only.

**Verification:**
- README/CLAUDE.md accurately describe the auto-load + `--config` override; no stale
  "must pass `--config`" implication remains.

## System-Wide Impact

- **Interaction graph:** `Ctx.__init__` feeds **every** operator command
  (crawl/ingest/process/review-packet/approve/reject/resolve/backfill/supersede/
  list/run). Changing its config resolution changes the config all of them see when
  run without `--config` in a dir containing `config.yaml`. `init` and `gui` do not
  build `Ctx` and are unaffected.
- **Error propagation:** unchanged — `load_config` still raises `InputValidationError`
  (exit 2) for an explicit missing path; the new branch only adds an exists-gated
  default and never raises on its own.
- **State lifecycle risks:** none — read-only config resolution; no new writes.
  `base_dir`/store/audit paths already derive from `--output-dir or config.base_dir`,
  so they correctly track the now-loaded config.
- **API surface parity:** the GUI already auto-resolves (PR #12); this brings the CLI
  to parity. The intentional implementation asymmetry (GUI unconditional vs CLI
  exists-gated) is documented in Key Technical Decisions.
- **Integration coverage:** the new Unit-1 integration test is the first to drive the
  no-`--config` + cwd-`config.yaml` path end-to-end.
- **Unchanged invariants:** `load_config` semantics, the `Config` model, every
  compliance gate (reviewers/allow_domains), exit-code contract, and explicit-path
  behavior all stay exactly as-is.

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Behavior becomes cwd-dependent — a no-`--config` command run from a dir with a stray (often gitignored) `config.yaml` silently uses it | Med (a `config.yaml` already sits in the repo root from GUI dogfooding) | Med | This is the intended workspace convention (matches `init` + git/npm); document it (Unit 2). For tests, the new test `chdir`s to a clean tmp; the two affected existing tests are pinned (named in Open Questions/Unit 1) and the suite runs from a clean cwd. CI (clean checkout, gitignored `config.yaml` absent) is unaffected. |
| **Trust-by-location** — a cwd `config.yaml` is loaded by *location*, so running `lcp` inside a directory the operator did not author (unpacked archive, cloned/shared repo, downloaded job folder) silently adopts that file's `crawler.allow_domains` (the SSRF/crawl allowlist) and `publisher.reviewers` (sign-off authority) — the two compliance-critical settings | Low | Med | Single-operator local-tool trust model: the operator is assumed to control their cwd; `config.yaml` is gitignored (never travels via the repo), secrets are keyring/env-only (never in the file), and defaults are *restrictive* (empty allowlist rejects every crawl, no reviewers blocks every sign-off) so an absent or planted file fails closed/safe, not open. Accepted residual; Unit-2 doc advises running `lcp` from a known/own working directory (or `--config` when unsure). |
| A naive `or "config.yaml"` default (not exists-gated) breaks every no-`--config` command in a fresh/CI dir by raising "config not found" | Low (called out) | **High** | Exists-gate is the explicit Key Technical Decision; the error-path test (`Ctx({})` with no file → defaults, no raise) guards it. |
| Existing tests that build `Ctx` without `--config` and without `chdir`, expecting defaults, shift behavior when a local `config.yaml` is present | Low–Med | Med | Audit during implementation; prefer `monkeypatch.chdir(tmp)` in such tests; CI remains green because no `config.yaml` is committed. |

## Documentation / Operational Notes

- Operator-facing: after `lcp init`, plain `lcp run`/`process`/`approve` now use the
  scaffolded `config.yaml` — no `--config` needed (it remains available to point at a
  different file). Running `lcp` from a different directory uses that directory's
  `config.yaml` (or defaults if none).

## Sources & References

- Related code: `src/lcp/cli.py` (`Ctx`, `init`), `src/lcp/adapters/storage/config_io.py`
  (`load_config`), `src/lcp/webserver.py` (`_make_api`, the GUI twin fix).
- Related PRs: #12 (GUI twin — `webserver._make_api` config-path resolution).
- Institutional learnings: `docs/solutions/unit-tests-mask-integration-bugs.md`,
  `docs/solutions/real-happy-path-unreachable-masked-by-green-tests.md`.
