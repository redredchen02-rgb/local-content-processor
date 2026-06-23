---
title: "refactor: collision-free parallel optimization sweep"
type: refactor
status: active
date: 2026-06-23
origin: multi-agent discovery workflow (15 file-disjoint finders + adversarial verifiers)
---

# refactor: collision-free parallel optimization sweep

## Summary (结论先行)

A 16-agent discovery-and-verification pass swept the whole repo — both packages
(`src/lcp/**` and `gossip_scraper/**`) plus CI, docs, and tests — partitioned
into **file-disjoint zones**, then **adversarially verified** every finding and
mapped the cross-zone collisions. Result: **101 verified-real optimizations**
(plus ~19 single-finder findings in two zones whose verifier was interrupted),
**13 findings rejected** by the verifier as false/already-done, and a complete
**collision graph** that tells us exactly which work can run in parallel without
two lanes fighting over the same file.

The headline: most lanes are **file-disjoint and parallelizable**. The collisions
cluster into a small, predictable set of seams:

- **`src/lcp/core/rules/extraction.py` is a symbol magnet** — crawler, llm, media,
  processor all import from it. Safe to parallelize *only if* its public
  signatures are frozen during the wave.
- **Two file-overlap pairs must be pre-merged into single lanes**: `media`+`processor`,
  and `gossip-core`+`gossip-governance-cleanup`.
- **`app.js` and `assembler.py`/`copywriter.py` are shared with the HUB** — the HUB
  lane must stay off them; the web/llm lanes own them.
- **`pyproject.toml` is touched by both docs and governance** — serialize on one owner.
- **The HUB lane (`pipeline/cli/gui/webserver/container`) imports from everything** —
  it is the serialized spine and runs *after* the parallel wave.

This is a **planning document**, not an execution log. It defines the lanes,
the order, and the contracts that keep parallel work from colliding. Nothing
here is committed yet.

### The battle map at a glance

| Wave | Lanes (run simultaneously within a wave) | Why parallel-safe |
|------|------------------------------------------|-------------------|
| **A — parallel** | L1 core/rules · L2 crawler · L3 storage · L4 llm · L5 media+processor · L6 publisher · L7 web · L8 gossip-scrapers · L9 gossip-core+gov-cleanup | File-disjoint subtrees; cross-imports held to frozen signatures |
| **B — serialized spine** | L10 HUB (pipeline/cli/gui/webserver/container) → L11 gate-flip (bring gossip under mypy/ruff) → L12 e2e tests | HUB imports from all of Wave A; gate-flip needs gossip clean first; e2e wants source churn settled |
| **C — docs (anytime, pull httpx forward)** | L13 docs + CI/pyproject | Mostly markdown; one **shipping bug** (`httpx` undeclared) must land early |

---

## Problem Frame

After the gossip pipeline (plan-001), engine-extensibility refactor (plan-003),
and distribution/devex work (plan-004) all merged to `main`, the repo carries
the usual post-merge sediment: duplicated helpers, hardcoded knobs that should
be config, O(n²) hot paths at realistic scale, a second top-level package
(`gossip_scraper`) sitting **entirely outside the mypy/ruff/CI gates**, and
documentation that drifted from the shipped surface.

None of this is a bug-hunt — the gate chain and state machine are sound. It is a
**quality + performance + governance sweep**. The risk in doing it is not
correctness; it is **wasted parallel effort and merge conflicts** if independent
lanes touch the same files. This plan exists to make the sweep *parallelizable
without collisions*.

---

## How this plan was produced (provenance)

- A workflow (`parallel-safe-optimization-map`) spawned **one finder per
  file-disjoint zone** (14 zones), each returning findings + touched files +
  hub-collision flags, then **adversarial verifiers** per lane that (a) tried to
  refute each finding and (b) computed cross-lane file/symbol collisions.
- The run was interrupted (session transfer) **after** discovery + verification
  completed but **before** the final synthesis step ran, so the tool's return
  was empty. The complete per-agent results were **recovered from the workflow
  journal** (`wf_a4fb179b-1fe/journal.jsonl`): 14 discovery zones + 12 verified
  lanes joined by finding-id.
- **Verification did real work** — it dropped findings that didn't survive
  scrutiny (e.g. the media zone's `wire-or-cut-detect-silence` and the llm zone's
  `config-driven-token-budget` headliners were *not* carried into `verified_real`),
  and explicitly rejected 13 with written reasons (see audit trail below).
- **Two zones (`HUB`, `docs/`) have discovery findings but no completed verifier**
  (those agents were among the interrupted ones). They are listed as **Tier-2 —
  verify before acting**. I spot-checked the three cheapest/highest-impact docs
  claims by hand (see Tier-2 section): one confirmed a real shipping bug, one was
  stale/wrong and is dropped, one is real but its replacement number must be
  measured.

---

## The Collision Graph (verified)

Edges below are the verifier-reported cross-lane couplings. `file-overlap` =
both lanes edit the same file (hard conflict → must serialize or merge).
`symbol-coupling` / `import-dependency` = one lane owns a symbol the other
imports (safe to parallelize **iff** the owner freezes the public signature).

```
core/rules ──owns extraction.py──▶ crawler, llm, media, processor   [symbol-coupling]
core/rules ──file-overlap──▶ HUB                                     [serialize app-level]
media ◀──file-overlap + test-overlap──▶ processor                   [MERGE into one lane]
gossip-core ◀──file-overlap (9 files) + test-overlap──▶ gossip-gov  [MERGE into one lane]
gossip-scrapers ──imports──▶ gossip-core                            [symbol-coupling]
web ──file-overlap (app.js)──▶ HUB                                  [HUB stays off app.js]
llm ──file-overlap (assembler.py, copywriter.py)──▶ HUB            [HUB stays off them]
processor ──symbol-coupling──▶ publisher                            [freeze signatures]
publisher ──import-dependency──▶ storage                            [freeze signatures]
e2e ──symbol-coupling──▶ publisher, storage                         [test-only; write last]
docs ◀──pyproject.toml──▶ governance                                [serialize on one owner]
```

**Reading it:** the only *hard* (file-overlap) conflicts are the four marked
above. Everything else is import/symbol coupling that a **signature-freeze
contract** (below) neutralizes — which is what makes Wave A genuinely parallel.

---

## Parallel Execution Waves

### Wave A — the parallel wave (9 lanes, file-disjoint)

Each lane edits only its own subtree. The two file-overlap pairs are pre-merged.
Run all nine concurrently under the **signature-freeze contract**.

- **L1 — `src/lcp/core/rules`** (10 findings). The symbol magnet. May refactor
  internals freely but **MUST NOT** change the public signatures of
  `extraction.classify_media_url` / `extraction.extract_content` (incl. the
  `is_media_url_safe=` keyword and returned dict keys `image_urls` / `video_urls`
  / `rejected_media_urls` / `malformed_media_urls`) during the wave.
- **L2 — `src/lcp/adapters/crawler`** (9). Imports extraction.py read-only.
- **L3 — `src/lcp/adapters/storage`** (9).
- **L4 — `src/lcp/adapters/llm`** (8). **Owns** `assembler.py` + `copywriter.py`;
  HUB must not touch them this wave.
- **L5 — `src/lcp/adapters/media` + `src/lcp/adapters/processor`** (5 + 7 = 12).
  Pre-merged: `media_checker` imports `media`, and they share test files.
- **L6 — `src/lcp/adapters/publisher`** (6).
- **L7 — `src/lcp/web`** (11). **Owns** `app.js`; HUB must not touch it this wave.
- **L8 — `gossip_scraper/scrapers`** (9). Imports gossip-core read-only.
- **L9 — `gossip_scraper/core` + gossip mypy/ruff cleanup** (12 + cleanup).
  Pre-merged: the governance lane's gossip type/lint fixes edit the *same* 9 core
  files, so logic fixes and cleanup land together here.

### Wave B — the serialized spine (after Wave A)

- **L10 — HUB** (`pipeline.py`, `cli.py`, `gui.py`, `webserver.py`,
  `container.py`; **not** app.js / assembler / copywriter). 10 Tier-2 findings.
  Runs after Wave A because it imports from nearly every Wave-A module and is the
  integration point; landing it last avoids re-resolving against churn.
- **L11 — gate-flip** (bring `gossip_scraper` under mypy `files` + ruff, lift the
  `pyproject` "mid-edit" exclusion). **Depends on L9** making gossip type/lint
  clean — flipping the gate before that turns CI red.
- **L12 — `tests/e2e`** (7 findings, all new test files). Depends on stable
  signatures from publisher/storage/processor; best written once Wave A churn
  settles so tests aren't rewritten mid-flight.

### Wave C — docs (parallel to everything, but pull one item forward)

- **L13 — docs + CI/pyproject** (Tier-2). Mostly markdown and can run anytime.
  **Exception — land immediately, independent of waves:** `httpx` is imported by
  `gossip_scraper` but **declared nowhere** in `pyproject.toml` → a clean
  `pip install` of the gossip path breaks. This is a real shipping bug; fix it in
  its own tiny PR now. (Note: this edits `pyproject.toml`, the same file as L11's
  gate-flip — serialize the two pyproject edits on a single owner.)

---

## Lane-by-lane verified findings

Severity/category as returned by the verifier. IDs map 1:1 to the recovered
journal; full rationale/evidence per finding is in the workflow journal.

### L1 — core/rules (10 verified)
- `dedup-lsh-cache-not-thread-safe` **[high/robustness]** — module-global LSH
  caches mutated without a lock; sync+async pipeline can interleave eviction with
  read → `KeyError`. A pure rule module holding shared mutable state is the smell.
- `dedup-fingerprint-truncation-collision` [med/robustness]
- `dedup-stage1-recomputes-entry-hashes` [med/perf]
- `extraction-accept-dedup-quadratic` [med/perf]
- `risk-substring-multilingual-false-positive-gap` [med/robustness]
- `risk-haystack-built-twice` [low/perf]
- `lint-dedup-paragraph-recompute` [low/quality]
- `grounding-substring-cost-long-claims` [low/perf]
- `grounding-split-claims-empty-strategy-noop` [low/perf]
- `apply-uncertainty-tone-marker-coupling` [low/quality]

### L2 — crawler (9 verified)
- `max-assets-not-forwarded-to-subprocess` **[high/robustness]** — CLI builds
  `SourceSpec(max_assets=config…)` but `crawl_url` never forwards it to the
  subprocess and `scrapy_impl` has no `--max-assets` flag → URL crawls silently
  cap at the default 100, diverging from the local-ingest path. Add the flag +
  thread it through.
- `open-crawl-mode-default-empty-allowlist` [med/robustness]
- `subprocess-stderr-discarded` [med/robustness]
- `subprocess-spawn-oserror-uncaught` [med/robustness]
- `source-registry-untested-and-redundant-scan` [med/test]
- `redundant-dns-resolution` [low/perf]
- `scrapy-impl-full-file-read-into-memory` [low/perf]
- `media-url-guard-double-validates-in-extraction-then-download` [low/perf]
- `ingest-subfolder-and-empty-not-counted-undertested` [low/test]

### L3 — storage (9 verified)
- `sqlite-synchronous-normal` [med/perf] — WAL is on but `synchronous` stays at
  FULL (fsync per COMMIT on the hottest gate-landing path). NORMAL is the
  documented crash-safe WAL value; set it once in `_connect`.
- `makejobid-truncated-digest-collision` [med/robustness]
- `manifest-atomic-write-duplicates-fs-helper` [med/quality]
- `audit-verify-chain-full-file-memory` [med/perf]
- `init-db-redundant-per-instance` [low/perf]
- `audit-aggregate-streaming-input` [low/perf]
- `fsync-dir-every-append` [low/perf]
- `source-store-non-atomic-deletes` [low/quality]
- `delete-job-row-deleted-without-dir` [low/test]

### L4 — llm (8 verified)
- `unbounded-source-into-prompt` [med/perf]
- `nli-redundant-per-claim-sanitize` [med/perf]
- `nli-unbounded-claim-call-fanout` [med/perf]
- `verbatim-quote-extraction-quality` [med/quality]
- `init-all-export-drift` [low/quality]
- `nli-protocol-signature-divergence` [low/typegate]
- `config-token-cap-passthrough-test` [low/test]
- `client-broad-except-narrowing` [low/robustness]

### L5 — media + processor (5 + 7 verified)
media:
- `run-launch-failure-not-wrapped` [med/robustness]
- `to-int-negative-zero-asymmetry` [low/robustness]
- `clamp-watermark-font-size` [low/robustness]
- `anchor-xy-far-edge-overflow` [low/robustness]
- `blackdetect-real-timeout-test-parity` [low/test]

processor:
- `dedup-atomic-write-helper` [med/quality] — `media_checker._write_0600_json`
  reimplements canonical `atomic_write_0600` with a weaker `getpid()` temp name
  (PID-collision footgun); replace with the audited `_fs` primitive (~22 lines
  deleted).
- `cover-advisory-untested-in-zone` [med/test]
- `oversized-video-skips-probe` [low/perf]
- `has-images-counts-pre-normalization` [low/robustness]
- `dedup-index-read-text-whole-file` [low/perf]
- `sanitize-sections-roundtrip-waste` [low/quality]
- `persist-gate-state-return-discarded` [low/quality]

### L6 — publisher (6 verified)
- `render-message-omits-bound-sections` **[high/robustness]** — `_draft_body_text`
  binds subheads + image/video captions into `body_sha256`, but `_render_message`
  never renders them → the reviewer signs off on (and is hash-bound to)
  AI-generated content the packet hides. Purely additive rendering (escaping path
  already exists).
- `read-manifest-unhandled-json-decode` [med/robustness]
- `supersedable-never-signed-off-drift-guard` [med/test]
- `blocking-codes-full-audit-scan` [low/perf]
- `redline-codes-empty-no-test` [low/test]
- `init-missing-resolve-export` [low/quality]

### L7 — web (11 verified)
- `open-job-serial-awaits` [med/perf] — `openJob()` fires 5 sequential `/api`
  round-trips; reviewers/get_packet/cover_report are independent → `Promise.all`
  roughly halves time-to-interactive on the most-clicked screen.
- `duplicate-get-settings` [med/perf]
- `stale-render-on-rapid-nav` [med/robustness]
- `confirm-tray-disarm-aria` [med/quality]
- `no-js-asset-source-test-coverage` [med/test]
- `armed-tray-cancel-leak` [low/robustness]
- `no-poller-cleanup-on-nav` [low/perf]
- `advisory-baseurl-no-debounce` [low/perf]
- `capfor-dead-indirection` [low/quality]
- `job-row-redundant-open-button` [low/quality]
- `poll-cap-comment-wrong` [low/docs]

### L8 — gossip scrapers (9 verified, 1 rejected)
- `shared-http-fetch-helper` **[high/quality]** — all 16 scrapers hand-roll the
  identical `httpx.AsyncClient(timeout=15)` + `raise_for_status()` + a copy-pasted
  `_HEADERS` (UA already drifted Chrome/120 vs /125). Extract `fetch_json`/
  `fetch_text` into `base.py`; it becomes the single seam where retries +
  redirect-following land once instead of 16×.
- `follow-redirects-false` [high/robustness]
- `rss-regex-parser-dup-and-fragility` [high/quality]
- `untested-scrapers-coverage-gap` [high/test]
- `no-html-entity-unescape` [med/robustness]
- `no-transient-retry` [med/robustness]
- `tag-from-title-dup` [med/quality]
- `douban-celeb-movie-near-duplicate` [med/quality]
- `loose-dict-annotations` [low/typegate]

### L9 — gossip core + governance cleanup (12 + cleanup verified)
core:
- `velocity-uses-preranking-rank` [high/quality]
- `generator-summary-precedence-bug` [high/robustness]
- `thin-test-coverage-enrichers` [high/test]
- `dedup-quadratic-lcs` [med/perf] — O(n²) pairwise dedup, each pair allocating a
  full (L+1)×(L+1) LCS matrix; ~700 items at default full-platform scale →
  ~245k comparisons. Roll the DP to two rows (or rapidfuzz).
- `summary-rsplit-truncation` [med/robustness]
- `category-duplicate-keywords` [med/quality]
- `freshness-cross-platform-rank-mix` [med/quality]
- `snapshot-io-robustness` [med/robustness]
- `snapshot-duplicate-pipeline` [med/quality]
- `health-full-file-reread-per-record` [low/perf]
- `max-scores-get-typegate` [low/typegate]
- `models-todict-description-asymmetry` [low/quality]

governance (cleanup that edits the same files — lands here):
- `gossip-mypy-prereq-cleanup` **[high/typegate]** — the package emits ~10 mypy
  errors (var-annotated dashboard dicts; `max(scores, key=scores.get)` arg-type
  ×3; alerts call-overload; snapshot attr-defined). **This is the ordering
  dependency** the gate-flip (L11) hinges on — clean *then* flip.
- `gossip-ruff-prereq-cleanup` [high/quality]
- `snapshot-reuse-scraperprotocol` [med/quality]
- `gossip-mypy-strict-followup` [low/typegate]

### L11 — gate-flip + CI (governance, Wave B)
- `ci-ruff-unpinned` [med/robustness]
- `ci-no-pip-cache` [med/perf]
- `release-benchmark-no-gate` [med/robustness]
- `ci-no-concurrency-cancel` [low/perf]
- (the flip itself: add `gossip_scraper` to `[tool.mypy] files` + ruff, lift the
  stale `pyproject` exclusion — only after L9 is clean.)

### L12 — e2e (7 verified)
- `e2e-blocked-supersede-recovery` **[high/test]** — no e2e drives a *real*
  redline source → BLOCKED → `supersede(redline_override=True)` → SUPERSEDED
  through the real gate chain; every existing supersede test seeds BLOCKED via the
  `persist_gate_state` shortcut, so RiskCategory-code drift would be masked.
- `e2e-reject-path` [med/test]
- `e2e-audit-chain-verify` [med/test]
- `e2e-process-batch-real-chain` [med/test]
- `e2e-needs-revision-reprocess-recovery` [med/test]
- `e2e-private-audit-accessor` [low/quality]
- `e2e-tamper-asserts-state-unchanged` [low/robustness]

---

## Tier-2 — single-finder, verify before acting

These two zones' adversarial verifiers were interrupted. Treat as **leads, not
conclusions** — run a quick refute pass before each fix.

### L10 — HUB (10 discovery findings, unverified)
- `get-job-escapes-lookup-key` [high/robustness] — `Api.get_job` HTML-escapes the
  `job_id` *before* the DB lookup (every other method escapes on output only), so
  ids containing `& < > " '` query the wrong key. One-line fix; matches a real
  escape-on-output-only invariant.
- `crawl-ingested-not-inflight-guarded` [med/robustness]
- `adapters-container-dead-path` [med/quality]
- `completion-advisory-duplicated` [med/quality] — `_completion_advisory`
  byte-identical across `cli.py` + `gui.py`.
- `get-job-reconcile-full-scan` [med/perf]
- `internal-error-dict-duplicated` [low/quality]
- `extract-worklist-functions-from-pipeline` [low/quality]
- `lazy-persist-import-x3` [low/perf]
- `stopped-at-doc-omits-media` [low/docs]
- `process-batch-strands-processing-marker` [low/test]

### L13 — docs + pyproject (spot-checked by hand)
- ✅ **CONFIRMED REAL** `httpx-dependency-undeclared` [high] — `gossip_scraper`
  imports `httpx` (ptt/tieba/douyin/…), `pyproject.toml` declares it nowhere.
  **Land this fix immediately** (see Wave C).
- ✅ REAL `stale-test-count-claims` [med] — CLAUDE.md + CONTRIBUTING.md claim
  "~1043 tests"; the real number differs. **Measure with `pytest --collect-only -q`
  before writing the replacement number** (do not trust the finder's count).
- ❌ **DROP** `readme-docker-quickstart-broken-path` — finder claimed README
  points at `./material/demo-001`; in fact README + `docker-compose.yml` already
  use `./samples/demo-001`, which **exists**. Stale/wrong finding.
- (unverified, likely real) `gossip-scraper-undocumented` [high],
  `ingest-gossip-no-runbook` [high], `readme-layout-omits-new-modules` [med],
  `changelog-unreleased-missing-gossip-extensibility` [med],
  `active-plans-not-marked-shipped` [med],
  `pyproject-gossip-exclusion-comment-stale` [med],
  `missing-solutions-entries-promised` [med].

---

## Scope Boundaries

- **No new features** — this is a quality/perf/governance sweep only.
- **No auto-publish** — that's plan-002's (blocked) domain.
- **No gossip feature breadth** — covered by plan-001.
- **No engine-architecture changes** — gate registry / injection container / batch
  worklist are plan-003's domain. (The `adapters-container-dead-path` HUB lead is
  about *unused* dual-path complexity, not redesigning the container — verify
  before touching.)
- **No state-machine edges** — the freeze-by-edge-absence and BLOCKED/DUPLICATE
  terminality invariants are untouched.
- **Signature-freeze contract holds for the whole of Wave A** (see below).

---

## Sequencing rules & contracts (what keeps it collision-free)

1. **Signature-freeze contract (Wave A).** Any lane that *owns* a cross-imported
   symbol may change its internals but not its public signature during the wave:
   - L1 freezes `extraction.classify_media_url` / `extract_content`.
   - L6 (publisher) / L3 (storage) freeze the symbols L12/e2e and each other import.
   - L9 (gossip-core) freezes the symbols L8 (scrapers) imports.
   Signature changes, if any, move to Wave B as an explicit HUB-serialized step.
2. **Pre-merged pairs are single lanes, single owner:** media+processor (L5),
   gossip-core+gossip-cleanup (L9). Do not split them across parallel agents.
3. **HUB stays off shared files** during Wave A: no edits to `app.js` (L7 owns),
   `assembler.py`/`copywriter.py` (L4 owns).
4. **`pyproject.toml` has one owner.** The `httpx` fix (Wave C) and the gate-flip
   (L11) both edit it — serialize them; don't run as two parallel agents.
5. **Gate-flip after clean.** L11 (add gossip to mypy/ruff) only after L9 makes
   gossip type/lint-clean, or CI goes red.
6. **e2e last.** L12 writes new test files against post-churn signatures.
7. **Each lane is its own PR/commit**, mergeable independently — that's the payoff
   of the partition. Wave A's nine lanes can be nine concurrent branches.

---

## Rejected findings (verifier audit trail)

Recorded so they are not re-discovered. The verifier rejected these with reasons:

- `stale-orphan-pyc` (gossip-scrapers) — the orphan `.pyc` is gitignored & untracked;
  nothing to remove from version control.
- `asset-rules-test-coverage-cover-video-gaps` (core/rules) — `judge_black_segments`
  and `judge_cover` boundary cases are already covered.
- `summarize-gaps-divzero-guard` (storage) — buckets always have ≥1 element;
  the claimed ZeroDivisionError cannot occur.
- `dedupe-cover-source-decode`, `missing-file-no-image-judge-rules` (media) —
  misreads; each source decoded once, missing-file branch already covered.
- `dedup-malformed-line-no-test`, `watermark-body-pass-untested-in-gate` (processor)
  — both already have direct unit tests.
- `duplicate-clear-calls-in-test-not-a-bug`, `manifest-source-links-inert-not-rendered-test`
  (publisher) — the recompute is the integrity check on purpose; inert-link test exists.
- `e2e-dry-run-real-chain`, `e2e-run-until-draft-target` (e2e) — both already
  exercised through the real chain in `test_pipeline_batch.py`.
- `readme-docker-quickstart-broken-path` (docs) — **rejected on hand spot-check**;
  README already uses the correct existing `samples/demo-001` path.

Headliners that discovery proposed but verification dropped (not in `verified_real`):
`wire-or-cut-detect-silence` (media), `config-driven-token-budget` (llm) — re-derive
before trusting.

---

## Suggested first PRs (highest value / lowest risk)

1. **`httpx` declaration** (Wave C exception) — one-line shipping-bug fix.
2. **L6 `render-message-omits-bound-sections`** — closes a real review-completeness
   hole in the freeze contract; additive rendering only.
3. **L1 `dedup-lsh-cache-not-thread-safe`** — latent crash under the documented
   sync+async path.
4. **L2 `max-assets-not-forwarded-to-subprocess`** — silent config-drop on the URL
   crawl path.
5. **L9 `gossip-mypy/ruff-prereq-cleanup`** — unblocks the whole governance
   gate-flip (L11).

Then fan out Wave A's nine lanes as concurrent branches under the contract above.
