---
title: "fix: Preserve crawl source URL + auto-populate re-crawl dialog for crawl_failed jobs"
type: fix
status: completed
date: 2026-06-24
---

# fix: Preserve crawl source URL + auto-populate re-crawl dialog for crawl_failed jobs

## Overview

When a standard URL crawl fails (`CRAWL_FAILED`), the source URL is completely lost — it is never persisted to disk, and the audit log prohibits URLs by design. The GUI's "重新抓取" affordance calls `openCreate(jobId)`, which opens the create dialog with the job ID pre-filled but the URL field blank. The operator must remember or re-locate the original URL before they can retry.

This plan closes the gap: persist the URL on first crawl, expose a read-only API to retrieve it, and auto-populate the re-crawl dialog when the URL is available.

## Problem Frame

- **Source URL only persisted for gossip-ingested jobs** (`gossip_ingest.write_source`). Standard URL crawl jobs (`create_and_crawl`) write nothing to disk.
- **`openCreate(jobId)` is synchronous and URL-blind**: it never calls any API to look up the original URL.
- **`crawl_ingested` is orphaned**: it exists in `gui.py` and reads from `source.json`, but lex.js `STATE_ACTIONS` never exposes it as a UI action — it is CLI-only in practice.
- **app.js has a syntax bug in working tree** (`$  $("settings-base-url")`) that must be committed.

Non-goal: this plan does not wire up `crawl_ingested` as a separate UI action for gossip `crawl_failed` jobs. That is a follow-up.

## Requirements Trace

- R1. When a standard URL crawl job is created, its source URL must be persisted in a way that survives a `CRAWL_FAILED` outcome.
- R2. On retry, the URL persisted in R1 must be retrievable via the API bridge without widening the PII surface of the SQLite index or audit log.
- R3. When the operator clicks "重新抓取" for a `crawl_failed` job, the URL field must be pre-populated with the persisted URL if one exists; if none exists, the field stays blank (current behavior).
- R4. On retry with a corrected URL, the persisted URL must be updated so subsequent retries pre-populate the corrected value.
- R5. Gossip `source.json` (which carries richer provenance: platform + title) must not be overwritten or degraded by any change in this plan.

## Scope Boundaries

- Does not change the audit log (PII-free invariant preserved).
- Does not change the SQLite schema.
- Does not add a CLI command (`get_source_url` is GUI-only, like `job_status`).
- Does not change the gossip ingest path or `crawl_ingested` behavior.
- Does not add a new `crawl_failed`-specific action in lex.js (follow-up work).
- CLI behavior changes (intentional): `lcp crawl` (CLI) will write `source.json` for standard URL jobs as a side-effect of the `pipeline.stage1` change — documented in Key Technical Decisions.

## Context & Research

### Relevant Code and Patterns

- `src/lcp/pipeline.py`: `Pipeline.stage1()` — the "SINGLE owner of Stage-1 sequence". Currently handles `CRAWL_FAILED → NEW` reset (bug_007 fix). Source URL (`spec.url`) is available here but never written.
- `src/lcp/adapters/storage/gossip_ingest.py`: `write_source()` (line 90), `read_source_url()` (line 103), `SOURCE_NAME = "source.json"` (line 31) — the established pattern for per-job URL persistence at 0600, crash-safe.
- `src/lcp/gui.py`: `create_and_crawl()` (line 189), `crawl_ingested()` (line 212) — two API methods that trigger stage1. `crawl_ingested` already calls `read_source_url` and feeds the URL to spec.
- `src/lcp/web/app.js`: `openCreate()` (line 1498) — synchronous; pre-fills job-id but not URL. `bindCreate()` (line 1520) — `btn-create` handler calls `create_and_crawl_async(jobId, url)`.
- `src/lcp/web/lex.js` `STATE_ACTIONS["crawl_failed"]` — single action `openCreate`. URL pre-population does not require lex.js changes.
- `tests/test_pipeline_batch.py:165` — existing `test_stage1_recrawl_allowed_for_crawl_failed` covers the state-machine edge; U2's test should extend this file.
- `tests/test_gui_api.py` — pattern for `Api` tests without a running server; U3's test goes here.
- `tests/test_cli_gui_parity.py` — `_GUI_ONLY` set; `get_source_url` belongs here.

### Institutional Learnings

- `docs/security/pii-inventory.md`: audit log is PII-free by construction; URL is a prohibited key. Source URLs belong in the per-job bundle (`data/jobs/<id>/source.json`, 0600), not in SQLite.
- `docs/solutions/`: `write_source` is already the crash-safe pattern for per-job URL persistence; reuse it rather than inventing a new file format.
- CLI/GUI parity rule (CLAUDE.md): read-only status helpers (`job_status`, `get_ingest_report`) do not need CLI mirrors — they are GUI polling tools. `get_source_url` is in the same category.

## Key Technical Decisions

- **Write source URL in `pipeline.stage1`, not in `gui.py`**: `stage1` is the single owner of the Stage-1 sequence; this ensures both `create_and_crawl` (GUI) and `lcp crawl` (CLI) benefit without duplication. `pipeline.py` already imports from `gossip_ingest` (`ingest_items`), so adding `write_source` / `SOURCE_NAME` to the import is incremental, not a new coupling.
- **Gossip guard via `platform` field**: gossip `source.json` has `platform ≠ "url"`. In stage1, check the existing `source.json.platform` before overwriting on retry: write freely when the file is absent (new job) or has `platform = "url"` (standard retry with possibly corrected URL); leave it untouched when `platform` is a real gossip platform (weibo/xiaohongshu/etc).
- **Two write sites in stage1**: (1) `existing is None` branch (new standard job): write on fresh creation; (2) `existing.state is CRAWL_FAILED` branch (retry): update if standard job. The `existing.state is NEW` fall-through (gossip jobs entering stage1 for first-time crawl) is deliberately not touched.
- **`get_source_url` is `_GUI_ONLY`**: read-only helper with no state mutation; like `job_status`. No CLI mirror needed. Decorated with `@bridge_safe` as all public Api methods are.
- **`openCreate` becomes async**: a short await after the form opens fetches the URL; UI is immediately visible while the fetch is in flight (best-effort, no error shown to user if lookup fails).

## Open Questions

### Resolved During Planning

- **Q: Should the URL write go in `gui.py::create_and_crawl` or `pipeline.stage1`?** A: `stage1` — it's the documented single owner of Stage-1; writing there means CLI users also benefit from URL preservation without any extra work.
- **Q: How to distinguish gossip `source.json` from standard URL `source.json`?** A: By the `platform` field. Gossip writes a non-"url" platform (e.g. "weibo"). Standard jobs write `platform="url"`. Reader (`read_source_url`) only cares about `.url`, so the field is diagnostic-only.
- **Q: Does `get_source_url` need a CLI mirror?** A: No. It is a read-only UI polling helper (like `job_status`). CLI users retry by re-running the command; they do not need an API lookup. Add to `_GUI_ONLY`.
- **Q: What if `get_source_url` is called for a job with no `source.json`?** A: Return `{"url": null, "found": false}` (no error, no state change). Callers treat null as "leave field blank".

### Deferred to Implementation

_(none — all questions resolved below)_

### Resolved During Review

- **Q: Is `platform` always present in gossip-written `source.json`?** A: Yes — `write_source` (line 90) always writes `{url, platform, title}`; the field is never absent in gossip-written files. Standard jobs write `platform="url"`. This is the discriminator.
- **Q: Does `write_source` accept `platform="url"`?** A: Yes — confirmed from signature `write_source(job_dir, *, url, platform, title)`; no downstream code treats `"url"` as an error.
- **Q: Guard logic for absent platform?** A: Fail-closed. If `source.json` exists but `platform` is absent or empty → treat as unknown → leave untouched. Only overwrite when `platform` is explicitly `"url"`. This prevents edge cases from gossip payloads or hand-crafted files.

## Implementation Units

```
U1 (no deps, standalone)

U2 (no deps)
  │
U3 (dep: U2)
  │
U4 (dep: U3)
```

- [ ] **U1: Commit the app.js syntax bug already in working tree**

**Goal:** Ship the one-line fix (`$  $("settings-base-url")` → `$("settings-base-url")`) that is already in the working tree so it is not orphaned.

**Requirements:** Prerequisite cleanliness.

**Dependencies:** None.

**Files:**
- Commit: `src/lcp/web/app.js` (already modified)

**Approach:**
- The diff shows a stray `$  ` prefix on the `settings-base-url` input listener binding. This is a logic bug: `$  $(...)` parses as two expressions (`$` evaluated and discarded, then `$(...)` bound correctly), so it is not a SyntaxError — the listener still binds, but the dead `$` prefix obscures intent. No behavioral change needed, only cleanup.

**Test scenarios:**
- Test expectation: none — syntax fix only; browser would have caught this; no behavioral test needed.

**Verification:**
- `git diff src/lcp/web/app.js` shows zero remaining hunks after commit.
- `grep -n '\$  \$' src/lcp/web/app.js` returns no results.

---

- [ ] **U2: Persist source URL in `pipeline.stage1` for standard URL crawl jobs**

**Goal:** Write `source.json` for `SourceType.URL` jobs so the URL survives a `CRAWL_FAILED` outcome and can be retrieved on retry.

**Requirements:** R1, R4, R5.

**Dependencies:** None.

**Files:**
- Modify: `src/lcp/pipeline.py` (stage1 method, imports)
- Modify: `src/lcp/adapters/storage/gossip_ingest.py` (if `write_source` needs to accept `platform=""` or a new simpler overload is needed — confirm during implementation)
- Test: `tests/test_pipeline_batch.py` (extend alongside `test_stage1_recrawl_allowed_for_crawl_failed`)

**Approach:**
- Extend the existing import on line 49 of `pipeline.py`: add `write_source, read_source_url, SOURCE_NAME` to the named imports from `gossip_ingest` (strict bundle — no implicit re-export).
- **Scheme allowlist check before any write:** `urlparse(spec.url).scheme.lower() in {"http", "https"}` — mirrors `gossip_ingest._valid_scheme`; log + skip silently if scheme is invalid (same best-effort pattern).
- **`existing is None` branch** (new standard job, after `store.create_job`): call `store.ensure_job_dir(spec.job_id)` first (job dir may not exist yet — required by `_atomic_write_0600`), then call `write_source(spec.job_dir, url=spec.url, platform="url", title="")` if `spec.source_type == SourceType.URL and spec.url`.
- **`existing.state is CRAWL_FAILED` branch** (retry): **write `source.json` BEFORE `store.set_state(→NEW)`** to avoid crash leaving NEW state with stale URL. Guard logic (fail-closed): read `platform` field directly from raw JSON; if file absent → write; if `platform == "url"` → overwrite (corrected URL); if `platform` is absent/empty/other → leave untouched (R5).
- **`existing.state is NEW` fall-through** (gossip jobs entering stage1 for first-time crawl): leave unchanged — standard URL jobs do not reach stage1 in NEW state after initial creation, so this branch is gossip-only.
- **Side-effect on `crawl_ingested`:** after U2, standard URL jobs will have `source.json`. `crawl_ingested` currently fast-fails on None from `read_source_url`; post-U2 it will read the URL and proceed as a gossip crawl. Guard: in U3's implementation, return `{"found": false}` for jobs with `platform == "url"` to prevent `crawl_ingested` from treating them as gossip. See also U3 approach.
- All writes use `write_source` / `_atomic_write_0600` — no new crash-safety or permission logic needed.

**Patterns to follow:**
- `gossip_ingest.py:write_source()` — crash-safe 0600 atomic write.
- `gossip_ingest.py:read_source_url()` — defensive reader that returns None on any error.
- `pipeline.stage1` existing `existing is None` / `CRAWL_FAILED` logic (lines ~280-295).

**Test scenarios:**
- Happy path: `stage1` with `SourceType.URL` spec (use a new `_spec_url(store, job_id, url=...)` helper following `test_gossip_injected_processing.py:36–43`), new job → `source.json` exists with `platform="url"` and correct URL.
- Retry path (CRAWL_FAILED): create URL job via stage1, advance to CRAWL_FAILED, call stage1 again with corrected URL → `source.json` updated; verify `set_state` happened AFTER `write_source` by asserting file content before state reset.
- Gossip guard (R5): pre-seed `source.json` with `platform="weibo"` → stage1 retry → file unchanged (`platform` / `title` not overwritten).
- Fail-closed guard: `source.json` with `platform` absent → leave file untouched (not treated as standard URL job).
- LocalDir: `stage1` with `SourceType.LOCAL_DIR` → no `source.json` written.
- Error path: `unittest.mock.patch("lcp.pipeline.write_source", side_effect=OSError("disk full"))` → assert stage1 returns without raising, crawl proceeds normally.
- Scheme guard: `spec.url = "file:///etc/passwd"` → no `source.json` written.

**Verification:**
- `test_stage1_recrawl_allowed_for_crawl_failed` still passes.
- New tests pass.
- `mypy` clean on `pipeline.py` (strict bundle) — no new untyped calls.

---

- [ ] **U3: Add `get_source_url(job_id)` API method to `gui.py`**

**Goal:** Expose a read-only bridge method so the GUI can retrieve a job's persisted URL without any state mutation.

**Requirements:** R2.

**Dependencies:** U2 (source.json must be written first for the method to be useful, though the method itself is independent).

**Files:**
- Modify: `src/lcp/gui.py` (add method near `crawl_ingested`)
- Modify: `tests/test_cli_gui_parity.py` (`_GUI_ONLY` set — add `get_source_url`)
- Test: `tests/test_gui_api.py` (new test function)

**Approach:**
- Add `@bridge_safe def get_source_url(self, job_id: str) -> dict:` after `crawl_ingested`.
- **Path traversal guard (P0):** call `c.store.get_job(job_id)` first; if None → return `{"found": False, "url": None}` immediately. This forces job_id to be a known DB key before any filesystem path is constructed.
- Only then: read raw JSON from `c.store.job_dir(job_id) / gi.SOURCE_NAME`; check `platform` field — if `platform != "url"` (gossip job), return `{"found": False, "url": None}` (prevents gossip jobs from pre-filling the standard create dialog with gossip URL). If `platform == "url"`, call `gi.read_source_url(...)` and return the URL.
- Return `{"url": url, "found": True}` if URL found — **do NOT `escape_html` the URL** because `app.js` sets it via `.value =` (property assignment, not innerHTML), and HTML-escaping would corrupt `&` characters in query params. The `.value =` path is safe by construction.
- Add `"get_source_url"` to `_GUI_ONLY` in `tests/test_cli_gui_parity.py`.

**Patterns to follow:**
- `crawl_ingested()` — reads `read_source_url`; same pattern, just without the crawl step.
- `job_status()` — read-only method that goes in `_GUI_ONLY`.
- `@bridge_safe` on every public Api method (mandatory per CLAUDE.md).

**Test scenarios:**
- Happy path: job with `source.json` (`platform="url"`) → returns `{"url": "https://...", "found": true}`.
- Missing: job with no `source.json` → returns `{"found": false, "url": null}`.
- Unknown job_id (path traversal guard): `get_source_url("../../etc/passwd")` or any non-existent job → DB lookup returns None → `{"found": false, "url": null}` (no filesystem access).
- Gossip job guard: job with `source.json` where `platform="weibo"` → returns `{"found": false, "url": null}` (prevents gossip URL from pre-filling standard create dialog).
- URL with query params: URL `https://example.com/a?x=1&y=2` → returned `url` is the raw string (not HTML-escaped) — verify `&` is not converted to `&amp;`.
- Parity: `get_source_url` must appear in `_GUI_ONLY` (test_cli_gui_parity asserts this).

**Verification:**
- `_GUI_ONLY` test passes (no "missing CLI mirror" failure).
- New tests pass.
- `mypy` clean on `gui.py` (non-strict shell; no new type errors).

---

- [ ] **U4: Auto-populate URL in `openCreate(jobId)` for re-crawl**

**Goal:** When the operator clicks "重新抓取" for a `crawl_failed` job, the URL input is pre-filled with the persisted URL (if any). This eliminates the need to remember or re-find the original URL.

**Requirements:** R3.

**Dependencies:** U3 (the `get_source_url` API must exist).

**Files:**
- Modify: `src/lcp/web/app.js` (`openCreate` function, ~line 1498)

**Approach:**
- Make `openCreate` an `async function`.
- **Synchronous setup first** (before any async work): show form, pre-fill job-id, and **explicitly clear the URL field** (`$("create-url").value = ""; $("create-source-pick").value = ""`) to prevent stale values from prior openings from flashing until the fetch resolves.
- **Enumerate callers** (grep `openCreate` in app.js and lex.js before implementing to confirm all call sites are event listeners or async contexts — required by async function declaration).
- **URL auto-populate (fire-and-forget, not `await`):** use a promise chain, not `await`, so the form is never suspended:
  ```
  if (jobId) {
    a.get_source_url(jobId)
      .then(res => {
        if (res && res.found && res.url && $("create-url").value === "") {
          $("create-url").value = res.url;
          // activate URL mode if needed (setUrlMode(true) or equivalent)
        }
      })
      .catch(() => {}); // silently swallow — form field stays blank
  }
  ```
  The guard `$("create-url").value === ""` prevents clobbering text the operator started typing while the fetch was in-flight.
- `loadSavedSources()` is still called fire-and-forget before the URL fetch. Reset `$("create-source-pick").value = ""` synchronously before `loadSavedSources()` so a stale saved-source selection doesn't overwrite the pre-populated URL via `applySavedSource`.
- All callers passing `null` (new job via nav / keyboard `n`) are unaffected — the `if (jobId)` block is guarded by truthiness.

**Patterns to follow:**
- `loadSavedSources()` — fire-and-forget promise pattern already in `openCreate`.
- `applySavedSource` — example of how saved-source auto-populates `$("create-url").value`; the same guard (`value === ""`) should apply there to avoid conflict.

**Test scenarios:**
- Test expectation: none for app.js directly — JS is not unit-tested; behavior is verified end-to-end in the running GUI. The backend API (U3) is already tested.
- Integration signal: after U2 + U3 + U4, an operator should be able to: (1) crawl a URL that fails, (2) click "重新抓取", and (3) see the URL pre-filled in the create dialog without any manual re-entry.

**Verification:**
- Launch `lcp gui`; create a job with a URL that returns a crawl error. Job lands in `crawl_failed`. Click "重新抓取". URL field is pre-filled with the original URL. Can clear/correct URL and retry successfully.
- `openCreate(null)` (new job via nav) — URL field remains blank; no `get_source_url` call is made (verified by absence of API request in browser DevTools Network tab).

## System-Wide Impact

- **Interaction graph:** `openCreate` now makes an async call to `get_source_url`. This is new network I/O inside the create dialog path, but it is best-effort and does not block the form from appearing.
- **Error propagation:** `write_source` failures in stage1 must not abort the crawl — log and continue. `get_source_url` failures in the GUI are silently swallowed (form field stays blank).
- **State lifecycle risks:** Writing `source.json` before the crawl succeeds means the file exists even for jobs that fail permanently. This is intentional — it enables retry. The file is part of the per-job bundle (gitignored, 0600) and does not affect the PII-free SQLite index.
- **API surface parity:** `get_source_url` is `_GUI_ONLY` — no CLI mirror. The test_cli_gui_parity.py test must be updated accordingly.
- **Unchanged invariants:** The audit log remains PII-free (no URL added). The SQLite schema is unchanged. `CRAWL_FAILED → NEW` retry edge in the state machine is unchanged. Gossip source.json metadata (`platform`, `title`) is preserved.
- **`source.json` mutability:** Standard URL job's `source.json` is mutable — on retry with a corrected URL, the file is overwritten and the original failed URL is not retained. This is intentional; forensic history is out of scope.
- **`crawl_ingested` after U2:** `crawl_ingested` previously fast-failed for standard URL jobs (no `source.json`). Post-U2, standard URL jobs will have `source.json`. `get_source_url` (U3) guards against this by checking `platform` and returning `{"found": false}` for `platform="url"` jobs — preventing the standard create dialog from treating gossip retry as a gossip crawl.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Gossip source.json overwritten by U2 | Fail-closed guard: absent/empty `platform` → leave untouched; explicit `platform="url"` → overwrite. Gossip guard also in U3 (`get_source_url` returns `found=false` for gossip jobs). |
| `write_source` failure during crawl setup | Best-effort: log the error, do not raise. Crawl proceeds; source URL won't be pre-filled on retry. Test with `mock.patch("lcp.pipeline.write_source", side_effect=OSError)`. |
| `openCreate` async — operator input clobbered by late-arriving fetch | URL field cleared synchronously first; pre-populate only when `$("create-url").value === ""` (field still blank) at fetch-complete time. |
| `openCreate` async — caller contract | Enumerate all call sites (grep) to confirm event-listener context before implementing `async` declaration. |
| Path traversal via `job_id` in `get_source_url` | DB existence check first (`get_job(job_id)`); return `{"found": false}` immediately if not in DB — no filesystem access for unknown job IDs. |
| `ensure_job_dir` missing for new jobs | `store.ensure_job_dir(spec.job_id)` called before `write_source` in `existing is None` branch. |
| Scheme-abusing URL persisted to source.json | Scheme allowlist check (`http`/`https` only) before `write_source` call; log + skip silently if invalid. |
| XSS via attacker-shapeable URL in source.json | `app.js` sets value via `.value =` (property assignment, not innerHTML) — safe by construction. Do NOT `escape_html` the URL in `get_source_url` (would corrupt `&` in query params). |

## Sources & References

- Related code: `src/lcp/pipeline.py:260` (`stage1`), `src/lcp/adapters/storage/gossip_ingest.py:90` (`write_source`), `src/lcp/gui.py:212` (`crawl_ingested`), `src/lcp/web/app.js:1498` (`openCreate`)
- Related tests: `tests/test_pipeline_batch.py:165` (`test_stage1_recrawl_allowed_for_crawl_failed`), `tests/test_gui_api.py:491` (`test_create_and_crawl_unknown_status_defaults_to_crawl_failed`), `tests/test_cli_gui_parity.py` (`_GUI_ONLY`)
- Institutional: `docs/security/pii-inventory.md` (URL prohibition in audit log), gossip ingest docstring (`gossip_ingest.py:1-15`)
