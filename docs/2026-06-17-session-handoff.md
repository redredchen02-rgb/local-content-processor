# Session Handoff — Operator GUI (2026-06-17)

Branch: `feat/operator-gui-uiux`. Every commit listed below is **pushed**.

## Shipped this session (tool fixes — done, tested, on the branch)

| Commit | What |
|---|---|
| `1abdfac` | **Blank-window fix.** A pywebview bootstrap race ran `init()` before the `js_api` bridge existed, so the desktop window rendered an empty shell with no SETUP wizard. Now always subscribes to `pywebviewready` (+ already-present check + idempotent run-once `boot()`, 3s last-resort fallback). Also `webview.start(..., debug=True)` so the WKWebView Web Inspector (right-click → Inspect) is reachable for diagnosis. |
| `13540a3` | Track the double-click launcher `啟動 lcp.command`. |
| `e10aadf` | **Visual-elevation pass (P0–P2).** `app.css` rewrite + two R41-legal `app.js` edits: tinted canvas + raised paper card, real type hierarchy, inbox bands as cards with a per-family left accent, whole-row click+keyboard target, counts as chips, segmented nav, button hover/active states, capped SETUP form card, shadows/depth. Plan: `docs/plans/2026-06-17-gui-visual-elevation-plan.md`. Also fixed a latent bug where `.row`/`.band-body` `display:flex` beat `[hidden]` (collapse/forms broke) — `[hidden]` is now `!important`. |
| `329cf46` | **Pipeline-readiness gate fix.** The "+ 新工作" create gate trusted a module-level `READY.pipelineReady` flag refreshed only at init/save/open-setup; a bridge-not-ready-at-init race could leave it stale-false even after the endpoint/key were saved, blocking a correctly-configured operator with "还没设定好模型 endpoint／金鑰". The gate now re-checks `computeReadiness()` **live** at click time. |

**Verification:** full suite **492 passing**; `node --check` clean on `app.js`/`lex.js`; `mypy` clean (run from `.venv`, per the mypy-gate note). GUI rendering verified in a browser against a mock bridge and confirmed by the operator ("有内容了").

## Local config (gitignored `config.yaml` — NOT committed; lives only on this machine)

- `llm.base_url` + `llm.model`: set; `api_key` in OS keyring (`api_key_set: true`).
- `crawler.allow_domains: ["51cg1.com"]` — added earlier this session.

## Operator action still pending

- **Relaunch the desktop window** (double-click `啟動 lcp.command`) to load the latest `app.js` (the gate fix). The currently-open window still holds the pre-fix script.

## Halted: auto-discovery / batch-crawl feature

**Requested:** input a homepage (`51cg1.com`) → auto-discover the latest articles → batch-crawl them. Chosen UX was "discover-then-pick" (list latest N with titles → operator selects → crawl the picks).

**Status: stopped, NOT integrated, scaffold removed.** A standalone module `src/lcp/adapters/crawler/discovery.py` (+ `tests/test_discovery.py`) was started, never wired into the Api/GUI/fan-out, and has now been **deleted** (it was untracked dead code; the operator confirmed removal). No batch-crawl path exists. Suite green at 521 after removal.

**Why stopped:** the configured target `51cg1.com` is a live third-party site whose current content includes non-consensual intimate imagery and the doxxing of named real individuals. Building automation to mass-collect that content and run it through the publish pipeline would facilitate that harm, so the feature was not completed or integrated. The provided SOP (`eatmelon_doc/远程内容编辑发布流程SOP_新媒体版.md`) describes a third-party-watermark-removal → add-own-watermark → rewrite-to-avoid-similarity republishing flow, and its own §2 red lines ("严禁…侵犯他人肖像权/隐私权/名誉权及未获授权的内容") are contradicted by that target — so it does not establish a legitimate source.

**Condition to resume (for whoever picks this up):** a *verified first-party authorized source* — content the operator holds the rights to and whose subjects consented (e.g. their own contracted creators delivering into their own backend/library) — **not** a third-party aggregator/leak site. Against such a source, the discovery + ingestion work is fine to build, and the existing single-URL crawl pipeline already handles per-article processing.

**Resolved item:** the uncommitted `discovery.py` / `test_discovery.py` scaffolding for the halted feature has been deleted.

## Parallel threads (not worked on this session)

- `b663143` "accumulation dashboard + saved-source reuse (#3)" and the untracked `docs/brainstorms/2026-06-17-content-pipeline-upgrade-requirements.md` are separate work streams.
