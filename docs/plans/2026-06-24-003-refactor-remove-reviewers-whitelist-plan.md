---
title: "refactor: Remove reviewers whitelist from config, signoff, and GUI"
type: refactor
status: active
date: 2026-06-24
deepened: 2026-06-24
---

# refactor: Remove reviewers whitelist from config, signoff, and GUI

## Overview

`PublisherConfig.reviewers: list[str]` is a whitelist that gates who can call
`approve`/`reject`/`resolve`/`backfill`. Empty list → no restriction (current
default). Non-empty list → only listed names pass. The feature is not needed:
there is no multi-user RBAC requirement, and the project deliberately stops
before publishing. An empty config breaks all sign-offs silently for operators
who skip the field. Remove the whitelist entirely — config field, enforcement
function, GUI endpoint, frontend UI, and all test fixtures.

**Attribution is kept.** The `reviewer: str` parameter on signoff functions and
`reviewer_stated` in `SignoffRecord` survive — they are audit trail, not
enforcement. CLI `--reviewer` option survives unchanged. GUI drops the reviewer
dropdown (no whitelist to populate it from); `Api.*` methods make `reviewer`
optional (default `""`). GUI-originated sign-offs record `reviewer_stated: ""`
in the audit log — this is intentional and acceptable; the OS user is still
captured in `observed_os_user`.

## Problem Frame

`_require_whitelisted()` returns early when `config.publisher.reviewers` is
`[]` (the default). If the list is populated, only those names can sign off.
The GUI readiness checker treats a non-empty list as P4 required — meaning
operators who don't configure it see a permanent "sign-off blocked" warning.
This is the opposite of the intended UX: the whitelist is an advanced compliance
feature that most operators should never need.

## Requirements Trace

- R1. `PublisherConfig.reviewers` field removed from config.py and config.example.yaml
- R2. `_require_whitelisted()` deleted; all 4 call sites (approve, reject, resolve, backfill) removed
- R3. `Api.reviewers()` deleted; `Api.approve/reject/resolve/backfill` make `reviewer` optional (default `""`)
- R4. GUI: `a.reviewers()` calls, reviewer dropdown, `reviewerSelect/reviewersEmpty/reviewerOnboarding` helpers, and P4 readiness row all removed
- R5. All test fixtures drop `reviewers=[...]` from `PublisherConfig()` constructors
- R6. Reviewer-specific tests removed; CLI docstring, help text, and lcp init guidance updated
- R7. mypy gate green; pytest suite green after removal
- R8. Operator runbooks updated: no reference to `publisher.reviewers` as a required sign-off prerequisite

## Scope Boundaries

- **Out**: Removing `reviewer: str` from signoff function signatures (backend attribution kept)
- **Out**: Removing `reviewer_stated` from `SignoffRecord` or audit events
- **Out**: Removing `--reviewer` from CLI (attribution still useful there)
- **Out**: Any other GUI changes beyond reviewer removal

## Context & Research

### Relevant Code and Patterns

- `src/lcp/core/config.py:107` — `PublisherConfig.reviewers: list[str]` field
- `src/lcp/adapters/publisher/signoff.py:153-160` — `_require_whitelisted()`
- `src/lcp/adapters/publisher/signoff.py:198, 329, 416, 536` — 4 call sites (approve, reject, resolve, backfill_published_url)
- `src/lcp/gui.py:634, 661, 680, 710` — `Api.approve/reject/resolve/backfill` (all take `reviewer: str`)
- `src/lcp/gui.py:915-921` — `Api.reviewers()` method
- `src/lcp/webserver.py:167-173` — routes auto-discovered from public `Api` methods; deleting `Api.reviewers` is sufficient to drop the `/api/reviewers` route
- `src/lcp/web/app.js:921-923` — `a.reviewers()` call inside `Promise.all` for job detail
- `src/lcp/web/app.js:1112-1150` — `reviewerSelect()`, `reviewersEmpty()`, `reviewerOnboarding()` helpers
- `src/lcp/web/app.js:1208-1422` — `renderActions()`, `buildActionRow()`, `holdPanel()` all receive/use `reviewers` param
- `src/lcp/web/app.js:1712-1782` — P4 readiness check + display row + config snippet
- `config.example.yaml:71` — `reviewers: []` with comment
- `src/lcp/cli.py:426` — "The reviewer must be in config.publisher.reviewers." docstring line

### Test files requiring fixture cleanup

Twenty-plus files pass `reviewers=[...]` to `PublisherConfig()`:
`tests/publisher/test_signoff.py`, `tests/test_gui_api.py`,
`tests/test_cli_config_discovery.py`, `tests/test_cli_skeleton.py`,
`tests/test_webserver_transport.py`, `tests/test_e2e_pipeline.py`,
`tests/test_live_llm_lane.py`, `tests/test_gossip_injected_processing.py`,
`tests/test_operator_guidance.py`, `tests/test_gui_settings.py`,
`tests/e2e/test_recovery_paths.py`, `tests/e2e/test_happy_path_dedup.py`,
`tests/e2e/test_full_flow_signoff.py`, `tests/e2e/test_quickstart_sample.py`

Tests to delete outright:
- `test_reviewers_returns_whitelist` in `tests/test_gui_api.py`
- `"reviewers"` entry in the parity endpoint list in `tests/test_gui_api.py`
- `test_approve_non_whitelisted_no_transition` in `tests/test_gui_api.py`
- `test_api_backfill_non_whitelisted_rejected` in `tests/test_gui_api.py`
- Inline mallory non-whitelisted check (lines 64-66) inside `test_full_signoff_loop_via_api` in `tests/test_gui_api.py`
- The mallory rejection sub-block (lines 113-118) inside `test_full_signoff_loop_via_cli` in `tests/test_cli_skeleton.py`
- `/api/reviewers` transport test (`test_no_arg_call_works`) in `tests/test_webserver_transport.py`
- `"reviewers"` from the hardcoded expected-routes set in `test_route_table_matches_independent_expected_list` in `tests/test_webserver_transport.py`
- `reviewer_stated == ["alice"]` round-trip assertion at `tests/test_gui_settings.py:88`

## Key Technical Decisions

- **Keep `reviewer: str` in signoff backend**: The backend functions are called
  from CLI with a meaningful name. Removing the param would break CLI and
  delete valuable audit attribution. Making it optional at the `Api` layer
  (default `""`) is the minimal change that decouples GUI from the param.

- **Auto-discovery makes webserver route drop free**: `public_routes()` in
  `webserver.py` reflects `Api` methods automatically. Deleting `Api.reviewers`
  removes `/api/reviewers` from the route table with no other change needed.

- **Remove reviewer dropdown entirely, not replace with text input**: The user
  confirmed "完全移除 reviewer 欄位" from GUI. `Api.approve/reject/resolve/backfill`
  gain `reviewer: str = ""` default; GUI calls them without the reviewer argument.

- **Remove P4 readiness row**: The readiness checker currently flags an empty
  whitelist as a blocking configuration gap. With the whitelist gone, P4 is
  meaningless. Remove the check and the display row; update the config snippet
  to only show `allow_domains`.

## Open Questions

### Resolved During Planning

- **Does `backfill` call `_require_whitelisted`?** Yes — `signoff.py:536`. Remove it.
- **Does webserver need explicit route deletion?** No — routes are auto-reflected from `Api` methods.
- **Can `reviewer: str` be removed from `Api` entirely?** No — `backfill` is also called from CLI
  (different code path via `cli.py`). Keeping `reviewer` in the backend and making it optional in
  `Api` is the right seam.
- **Does `PublisherConfig` have `extra="forbid"`?** No — no `model_config` set, Pydantic v2 default
  is `extra="ignore"`. Existing `config.yaml` files that still have `publisher.reviewers:` after
  the upgrade are silently ignored — no operator breakage.
- **`Api.reject(job_id, reviewer, reason)` param ordering:** Python cannot put a non-default `reason`
  after defaulted `reviewer`. Fix: reorder to `Api.reject(self, job_id: str, reason: str, reviewer: str = "")`.
  Update the frontend call from `a.reject(currentJobId, sel.value, reason.value)` →
  `a.reject(currentJobId, reason.value)`. Update the internal call body to use keyword args:
  `signoff.reject(job_id, reviewer=reviewer, reason=reason, ...)` to avoid positional mismatch
  after the signature reorder. The webserver dispatches args positionally; since there are no
  external consumers of `/api/reject` beyond the GUI, the reorder is safe.
- **`Api.resolve(job_id, reviewer, relint, reason)` param ordering:** Same issue as `Api.reject`.
  Fix: move `reviewer` to last position — `Api.resolve(self, job_id: str, relint: bool = False, reason: str | None = None, reviewer: str = "")`.
  Update the frontend call from `a.resolve(currentJobId, sel.value, relint, ...)` →
  `a.resolve(currentJobId, relint, ...)`. The internal call body does not need keyword args here
  since `relint` and `reason` keep their positions relative to `signoff.resolve`'s existing signature.

### Deferred to Implementation

- Whether `reviewer_stated: ""` in audit events causes downstream tooling issues (unlikely — the
  field is already present, just empty; not a breaking schema change).

## Implementation Units

- [ ] **U1: Remove `reviewers` from config schema**

  **Goal:** Delete the whitelist field from the config model and example file.

  **Requirements:** R1

  **Dependencies:** None — safe to land first; downstream code will fail mypy until U2/U3.

  **Files:**
  - Modify: `src/lcp/core/config.py` (line 107)
  - Modify: `config.example.yaml` (line 71)

  **Approach:**
  - Delete `reviewers: list[str] = Field(default_factory=list)` from `PublisherConfig`.
  - Delete the `reviewers: []            # whitelist of...` line from config.example.yaml.

  **Test scenarios:**
  - Test expectation: none — pure field removal; mypy + downstream tests catch breakage

  **Verification:**
  - `PublisherConfig()` no longer accepts a `reviewers` argument
  - mypy gate reflects the removed field

---

- [ ] **U2: Remove `_require_whitelisted` and all call sites in signoff.py**

  **Goal:** Drop whitelist enforcement from all four signoff functions.

  **Requirements:** R2

  **Dependencies:** U1 (config field gone — `_require_whitelisted` would be dead code anyway)

  **Files:**
  - Modify: `src/lcp/adapters/publisher/signoff.py`
  - Test: `tests/publisher/test_signoff.py`

  **Approach:**
  - Delete `_require_whitelisted()` (lines 153-160).
  - In `approve()` (line 198): remove the `try/except InputValidationError` block that wraps
    `_require_whitelisted`. The `except` body logs a `reviewer_not_whitelisted` audit event and
    re-raises — the entire try/except goes away.
  - Same removal pattern in `reject()` (line 329) — also a try/except block.
  - In `resolve()` (line 416): remove the **bare** `_require_whitelisted(config, reviewer)` call
    (no try/except wrapping here — same removal pattern as backfill, not approve/reject).
  - In `backfill_published_url()` (line 536): remove the bare `_require_whitelisted(config, reviewer)`
    call (no try/except wrapping here).
  - Update docstrings in `approve`, `reject`, `resolve`, `backfill_published_url`: remove "Refuses
    if reviewer is not whitelisted" sentences and "Whitelist-enforced like approve/reject" language.
  - Update the **signoff.py module docstring** (lines 5-6): change "picked from a config whitelist"
    to "caller-supplied attribution string (not validated — attribution only, not authentication)".
  - Update `test_signoff.py` module docstring if it mentions "whitelist enforcement".

  **Test scenarios:**
  - Happy path: `approve(reviewer="alice", ...)` succeeds without any whitelist check
  - Happy path: `reject(reviewer="alice", reason="...", ...)` succeeds
  - Happy path: `resolve(reviewer="alice", ...)` succeeds
  - Happy path: `backfill_published_url(reviewer="alice", ...)` succeeds
  - Attribution preserved: returned `SignoffRecord.reviewer_stated` equals the passed reviewer string
  - Audit event contains `reviewer_stated` key matching the passed reviewer
  - No "reviewer_not_whitelisted" audit event is ever emitted

  **Patterns to follow:**
  - The post-removal shape of `approve()` should look like `reject()` minus the try/except — straight-line logic from `observed_os_user()` into the state transition.

  **Verification:**
  - `_require_whitelisted` no longer exists in signoff.py
  - All four signoff paths accept any reviewer string without raising
  - Tests that previously set up `reviewers=["alice", "bob"]` now pass with no config change

---

- [ ] **U3: Remove `Api.reviewers()` and make `Api.approve/reject/resolve/backfill` reviewer-optional**

  **Goal:** Drop the whitelist endpoint; decouple the GUI-facing methods from a required reviewer string.

  **Requirements:** R3

  **Dependencies:** U2 (signoff functions no longer enforce whitelist)

  **Files:**
  - Modify: `src/lcp/gui.py`

  **Approach:**
  - Delete the `reviewers()` method (lines 915-921) including its `@bridge_safe` decorator.
  - `Api.approve`: add `reviewer: str = ""` default (no param reorder needed).
  - `Api.reject`: **reorder** to `reject(self, job_id: str, reason: str, reviewer: str = "")` —
    `reason` must precede `reviewer` because `reason` has no default and Python disallows non-default
    after default. Then update the internal call body to use keyword args:
    `signoff.reject(job_id, reviewer=reviewer, reason=reason, ...)`.
  - `Api.resolve`: **reorder** to `resolve(self, job_id: str, relint: bool = False, reason: str | None = None, reviewer: str = "")`.
  - `Api.backfill`: add `reviewer: str = ""` default (verify param ordering against signoff.backfill_published_url).
  - Update the `backfill` docstring (line 712) to remove "whitelisted reviewer" language.
  - No changes needed to `webserver.py` — route is auto-dropped.
  - **Note:** Deleting `Api.reviewers` immediately breaks the parity route-table guard test. U3 and
    U5 (which removes `"reviewers"` from the expected-routes set) must land in the same commit.

  **Test scenarios:**
  - `"reviewers" not in public_routes()` — verified by the parity endpoint list update in U5
  - `Api.approve(job_id)` (no reviewer arg) succeeds; `reviewer_stated` in response is `""`
  - `Api.approve(job_id, "alice")` still works (reviewer attr preserved if explicitly passed)
  - `Api.reject(job_id, reason)` (no reviewer) succeeds
  - `Api.resolve(job_id, relint=False)` (no reviewer) succeeds

  **Verification:**
  - `public_routes()` no longer includes `"reviewers"` (verifiable via a quick grep or the parity test update in U5)
  - `Api.approve()` callable without reviewer argument

---

- [ ] **U4: Remove reviewer UI from frontend**

  **Goal:** Strip all reviewer-related JS from app.js and associated HTML/CSS.

  **Requirements:** R4

  **Dependencies:** U3 (API endpoint gone)

  **Files:**
  - Modify: `src/lcp/web/app.js`
  - Modify: `src/lcp/web/index.html` (if any static reviewer element)
  - Modify: `src/lcp/web/app.css` (if any `.reviewer` rule)

  **Approach:**

  *Job detail load (around line 921-927):*
  - Change `const [ingestRes, reviewers, packetRes] = await Promise.all([a.get_ingest_report(jobId), a.reviewers(), a.get_packet(jobId)])` →
    `const [ingestRes, packetRes] = await Promise.all([a.get_ingest_report(jobId), a.get_packet(jobId)])`.
    Removing `a.reviewers()` shifts the array indices — the destructure must update in the same line
    or `packetRes` silently receives `undefined`.
  - Pass no `reviewers` arg into `renderActions()`.

  *Helper functions to delete entirely:*
  - `reviewerSelect()` (lines 1112-1120) — builds the `<select>` element
  - `reviewersEmpty()` (lines 1144-1145) — checks for empty list
  - `reviewerOnboarding()` (lines 1147-1150) — the onboarding banner

  *`renderActions(state, reason, reviewers)` (line 1208):*
  - Remove `reviewers` param and all `reviewersEmpty`/`reviewerOnboarding` guards.
  - Remove the `needsReviewer && reviewersEmpty(reviewers)` early-return block.
  - Pass no `reviewers` to `buildActionRow()`.

  *`buildActionRow(act, reviewers, reason)` (line 1268):*
  - Remove `reviewers` param.
  - Remove `reviewerSelect(reviewers)` calls from approve, reject, resolve branches.
  - Update `a.approve(currentJobId, sel.value)` → `a.approve(currentJobId)` (no reviewer arg).
  - Update `a.reject(currentJobId, sel.value, reason.value)` → `a.reject(currentJobId, reason.value)` (note: `reason` param order shifts — verify `Api.reject` signature).
  - Update `a.resolve(currentJobId, sel.value, relint, ...)` → `a.resolve(currentJobId, relint, ...)`.
  - Remove reviewer `<select>` from `holdPanel()` similarly.

  *`holdPanel(reviewers, reason)` (line 1394):*
  - Remove `reviewers` param; remove `reviewerSelect(reviewers)` call inside.
  - Update the `a.resolve(currentJobId, sel.value, relint, ...)` call inside `holdPanel` (line 1422):
    remove `sel.value` → `a.resolve(currentJobId, relint, relint ? null : reasonInput.value)`.

  *`computeReadiness()` function (around line 1712):*
  - Remove `const r = await a.reviewers()` (line 1719) — this is a second independent call site
    beyond the job-detail `Promise.all`. `computeReadiness` is called on every `afterAction()` and
    `saveSettings()` call; leaving it causes a 404 after every approve/reject/resolve.
  - Remove `const p4 = !isError(r) && !!(r.reviewers && r.reviewers.length > 0)` (line 1726).
  - Update `applyPill(p1 && p2, p4)` → `applyPill(p1 && p2, true)` — sign-off is now always-ready.
  - Remove `r.p4` from the result object returned by `computeReadiness`.

  *`renderReadiness()` function (around line 1762):*
  - Change `[r.p1, r.p2, r.p3 === true, r.p4].filter(Boolean).length` →
    `[r.p1, r.p2, r.p3 === true].filter(Boolean).length` and update the `/4` string to `/3`.
  - Change `else if (r.p3 !== true || !r.p4)` → `else if (r.p3 !== true)`.
  - Delete the `readyRow("审阅者白名单 reviewers", r.p4, ...)` display row (line 1773).
  - In the `pre.textContent` config snippet (line 1782), remove the `publisher:\n  reviewers:\n    - 你的名字` block.

  *CSS:*
  - Remove any `.reviewer` selector and associated rules if present.

  **Test scenarios:**
  - Test expectation: none — frontend logic; covered by the webserver transport test removal and manual smoke-test

  **Verification:**
  - `grep -n 'reviewers\b\|reviewerSelect\|reviewerOnboarding\|reviewersEmpty\|a\.reviewers' src/lcp/web/app.js` returns no matches
  - Approve/reject/resolve actions work in browser without reviewer input

---

- [ ] **U5: Clean up test fixtures and remove reviewer-specific tests**

  **Goal:** Remove `reviewers=[...]` from all `PublisherConfig()` calls; delete tests that exercised whitelist behavior.

  **Requirements:** R5, R6

  **Dependencies:** U1–U4 (field is gone; code no longer uses it)

  **Files:**
  - Modify: `tests/publisher/test_signoff.py` (line 42: remove `reviewers=[REVIEWER, "bob"]`)
  - Modify: `tests/test_gui_api.py`
    - Remove `test_reviewers_returns_whitelist` test
    - Remove `"reviewers"` from the parity endpoint list (line 72)
  - Modify: `tests/test_webserver_transport.py`
    - Remove the `/api/reviewers` transport test (lines 107-109)
  - Modify: `tests/test_cli_config_discovery.py`
    - Remove reviewer-loading assertions (lines 19, 22, 31, 33, 41, 47-49, 54-58, 68, 87, 91)
  - Modify: `tests/test_cli_skeleton.py` — remove `reviewers` from embedded YAML config dicts
  - Modify (remove `reviewers=` from fixture config): `tests/test_e2e_pipeline.py`,
    `tests/test_live_llm_lane.py`, `tests/test_gossip_injected_processing.py`,
    `tests/test_operator_guidance.py`, `tests/test_gui_settings.py`,
    `tests/e2e/test_recovery_paths.py`, `tests/e2e/test_happy_path_dedup.py`,
    `tests/e2e/test_full_flow_signoff.py`, `tests/e2e/test_quickstart_sample.py`

  **Approach:**
  - Grep for every `reviewers=` and `"reviewers"` in `tests/` and remove mechanically.
  - For `test_cli_config_discovery.py`: reviewer assertions must be **replaced**, not just deleted.
    Every test in this file uses `config.publisher.reviewers == [...]` as its sole correctness probe
    for cwd config auto-discovery. Simply deleting the assertion leaves the regression invisible.
    Replace each probe with a different config field: write `allow_domains: ['example.com']` in the
    test config fixture and assert `ctx.config.crawler.allow_domains == ['example.com']`.
  - Where a test YAML string contains a `publisher.reviewers:` key, delete that key and its list value.

  **Test scenarios:**
  - Happy path: full pytest suite (`pytest -q`) passes with zero reviewer-related failures
  - `PublisherConfig()` constructed without `reviewers=` in all fixtures

  **Verification:**
  - `grep -rn 'reviewers=' tests/` returns no matches
  - `pytest -q` green, count reduced by the deleted whitelist-specific tests

---

- [ ] **U6: Update CLI docstring**

  **Goal:** Remove whitelist language from the `approve` command help text.

  **Requirements:** R6

  **Dependencies:** None

  **Files:**
  - Modify: `src/lcp/cli.py` (lines 420-427)

  **Approach:**
  - Remove "The reviewer must be in config.publisher.reviewers." from the `approve` command docstring.
  - Remove "must be whitelisted" from all four `--reviewer` `@click.option` help strings (approve,
    reject, resolve, backfill). Replace with "Attribution label recorded in audit" or similar.
  - Update `backfill` command docstring: remove "This only RECORDS that a whitelisted human".
  - Update `cli.py:109` first-run guidance: remove "you have no reviewer whitelist/settings".
  - Update `cli.py:146` post-init guidance: remove "(add a reviewer)" from "Next: edit config.yaml".

  **Test scenarios:**
  - Test expectation: none — docstring/help text only

  **Approach (docs):**
  - Update `docs/2026-06-18-e2e-operator-runbook.md`: remove "publisher.reviewers — add at least
    one reviewer name (else approve/resolve/backfill refuse)" from setup section; remove the
    "config.yaml has ≥1 reviewer" go/no-go checklist item; remove reviewer from config snippet.
  - Update `docs/2026-06-18-session-handoff.md` line 19: remove "publisher.reviewers: [\"defuzi\"]
    — added so sign-off is no longer blocked."

  **Verification:**
  - `grep -n 'whitelist\|publisher.reviewers' src/lcp/cli.py docs/` returns no enforcement-language matches

## System-Wide Impact

- **Interaction graph:** `webserver.py`'s route table auto-reflects `Api` methods — deleting `Api.reviewers` atomically removes the `/api/reviewers` HTTP route with no other change.
- **Unchanged invariants:** `SignoffRecord.reviewer_stated`, audit event `reviewer_stated` key, CLI `--reviewer` option — all survive. Attribution is preserved; only enforcement is removed.
- **Parity surface:** CLI `lcp approve --reviewer alice` and GUI both call the same `signoff.approve()`. After this change, CLI passes a reviewer string (attribution); GUI passes `""` (no reviewer). Both paths are valid — backend accepts any string.
- **No state machine changes:** no new edges, no terminal state changes.
- **Config forward-compatibility:** `PublisherConfig` has no `model_config`; Pydantic v2 defaults to `extra="ignore"`. Existing `config.yaml` files that still have `publisher.reviewers:` are silently ignored after the upgrade — no operator action required.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| ~~Pydantic `extra="forbid"` breaks existing configs with `reviewers:`~~ | Non-issue — `PublisherConfig` has no model_config; Pydantic v2 defaults to `extra="ignore"` |
| Grep-based fixture cleanup misses an occurrence | Final `grep -rn 'reviewers=' tests/` as a verification step after U5 |
| `Api.reject` param reorder mismatched against `signoff.reject` internal call | Use keyword args: `signoff.reject(job_id, reviewer=reviewer, reason=reason, ...)` in U3 |
| Promise.all destructure index shift corrupts `packetRes` | Explicit destructure fix in U4 — remove index simultaneously with `a.reviewers()` call |
| U3 (delete `Api.reviewers`) + U5 (update parity set) must land in same commit | Route-table guard test fails the moment `Api.reviewers` is deleted; land them atomically |
| `config: Config` param becomes unused in `approve`, `reject`, `backfill` after U2 | mypy does not flag unused params, but callers still pass `config`. Deferred — can be cleaned up in a follow-on refactor if desired. |

## Sources & References

- Research scan: all `reviewers` occurrences across src/ and tests/ (2026-06-24)
- `src/lcp/adapters/publisher/signoff.py` — `_require_whitelisted`, approve, reject, resolve, backfill_published_url
- `src/lcp/gui.py` — `Api.reviewers`, `Api.approve/reject/resolve/backfill`
- `src/lcp/web/app.js` — reviewer UI, readiness P4 check
