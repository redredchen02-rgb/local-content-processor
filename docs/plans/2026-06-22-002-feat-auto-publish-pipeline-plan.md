---
title: "feat: Auto-publish pipeline (lcp → self-built backend)"
type: feat
status: blocked
date: 2026-06-22
origin: docs/brainstorms/2026-06-22-gossip-pipeline-core-requirements.md
predecessor: docs/plans/2026-06-22-001-feat-gossip-pipeline-core-plan.md
blocked_on: detector-calibration-spike, backend-review-queue-decision, backend-existence, copyright-acceptance-record
---

# feat: Auto-publish pipeline (lcp → self-built backend)

> **STATUS: BLOCKED — design + findings captured, NOT ready for `ce:work`.**
> This plan was split out of `…-001-…` during review because auto-publish (a)
> exceeds the operator's stated breadth-first priority, (b) depends on a safety
> control (output redline screening) that needs calibration work which is a
> gating deliverable in its own right, and (c) carries an unresolved product
> blocker. It captures the decided design and every review finding so the work
> is not lost — but **four prerequisites must be resolved before this is planned
> to completion** (see "Resolve Before Planning"). Re-enter via `/ce:plan` once
> they are.

## Overview

Add an **opt-in** machine path that publishes a processed, human-reviewable lcp
packet to a self-built backend over HTTPS, landing at a new `AUTO_PUBLISHED`
terminal state — removing the final human click. lcp's defining design is
"compliance-first, deliberately stops before publishing." This plan crosses that
line deliberately and only behind explicit opt-in, replacing each implicit human
control with a machine equivalent:

| Human control being removed | Machine replacement | Status |
|---|---|---|
| Redline screening of the *generated* text | Output-side redline re-screen | **needs calibration (P0 blocker)** |
| Post-freeze tamper re-read | `auto_approve` freeze-hash binding | designed |
| "Is this already posted?" | `.publishing` marker + backend idempotency | designed + needs lcp self-check |
| Reviewing the final artifact | (none — this is the irreducible removal) | the risk this plan accepts |

The honest framing review insisted on: this is **one deliberate opt-in** (a
coupled config decision plus a run flag), not "defense-in-depth." It should ship
only if the operator decides the convenience of removing one click is worth the
irreversible-publish risk — a decision the predecessor plan deliberately did not
force, since Phases 1–3 already deliver the stated value via the existing manual
one-click publish.

## Problem Frame

The predecessor plan (`…-001-…`) brings gossip jobs to `REVIEW_PENDING` with
cover + AI copy + watermark, where the operator publishes in one click via
`approve → backfill --attest`. The brainstorm's stated goal was full automation
("全自動：無需人點擊"). This plan is that automation — but review established it
should not ship until its safety control is calibrated and its product premise
(does the backend already have a human queue?) is confirmed. (see origin:
docs/brainstorms/2026-06-22-gossip-pipeline-core-requirements.md)

## Requirements Trace

- R12. New publisher path: HTTPS-only JSON POST to a configured backend endpoint; token in OS keyring; endpoint SSRF-guarded **and host-allowlisted**.
- R13. Opt-in auto-publish flow, no human click; `dry_run` still skips publish; **output redline re-screen (calibrated) replaces the human redline check**.
- R14. `--draft` flag: run LLM/cover/watermark, skip the publish POST; orthogonal to `--dry-run`. *(Review flagged overlap with `--until review`; justify a distinct operator use case or fold into tests — see Open Questions.)*

## Resolve Before Planning

These are true blockers carried from review. Do **not** finalize this plan or
start `ce:work` while any remain open.

1. **[P0 — Safety, research] Output redline detector is uncalibrated.** The sole
   machine replacement for the human's redline judgement is
   `risk_rules.assess_risk` with `KeywordRiskDetector`, self-described
   "CALIBRATION PENDING" — a substring match over ~6 short keyword tuples. An LLM
   rewrite that conveys a defamatory/NCII meaning *without* the listed tokens
   passes and auto-publishes. The codebase anticipates a claim-level NLI detector
   but **this plan must schedule it** (it is nowhere today). *Required before
   planning:* a calibration spike against an annotated Eatmelon 吃瓜 redline
   corpus that measures the detector's **recall** on the categories that matter
   (NCII, defamation, political), and a stated minimum recall bar below which
   auto-publish must not enable. Without a number, "replaces the human" is
   unfalsifiable.
2. **[P0 — Product] Does the self-built backend have its own human review
   queue?** If yes, "no manual step" is false — the human merely moves
   downstream, and inverting lcp's core invariant bought nothing. This single
   answer determines whether full-auto is worth building at all vs. "manual
   approve + auto-POST on approval." (Origin flagged this as Resolve-Before-
   Planning; it was never answered.)
3. **[P0 — Dependency] The backend does not exist yet.** The publisher adapter
   is shaped to a planning-defined contract. The contract's **idempotency-on-key
   guarantee is a hard precondition** (below); a backend that does not dedup
   makes auto-publish unsafe. Plan the adapter against the real contract once the
   backend exists, or accept that the adapter ships behind a verified-backend
   gate.
4. **[P1 — Legal] Copyright acceptance is not on record.** The plan must not
   assert this is resolved. Auto-publishing LLM rewrites of third-party gossip at
   Top-N scale, about named individuals, changes the copyright risk profile vs.
   human-reviewed publishing. *Required:* an explicit, recorded operator decision
   accepting the risk **for the auto-publish scale specifically**, or this stays
   a blocker.

## Key Technical Decisions (carried from deepening review — already validated against code)

- **New states: `AUTO_PUBLISHED` (terminal) + `PUBLISH_FAILED` (retriable → APPROVED) + operator-only `AUTO_PUBLISHED → SUPERSEDED` retract edge.** Distinct from `PUBLISHED_RECORDED` (machine vs human provenance). The retract edge records a takedown (mirrors the U8 BLOCKED lesson that terminal-with-no-recovery is a trap). `PUBLISH_FAILED` is **not** in `RECONCILABLE_STATES` (dead code — reconcile only fires on `.processing`-marked jobs).
- **Auto-publish transitions via `set_state` from APPROVED** (verified against `signoff.py:580 backfill_published_url`), **plus a distinct `.publishing` crash marker**: written before the POST, cleared after `set_state`. On reconcile, an APPROVED job with a stale `.publishing` marker is surfaced as "publish outcome unknown" → operator verifies, **never auto-retried**.
- **`auto_approve` replicates `approve`'s freeze-hash binding** (factor a shared `_verify_freeze_binding`): re-verify body/title/cover SHA against the manifest, record those SHAs in the `AUTO_APPROVED` audit event (actor=system), idempotency key = bound `body_sha256`. Machine sign-off is provably bound to the exact artifact, like human sign-off.
- **Output redline re-screen runs before `auto_approve`** over the assembled draft text (body + quick_facts + summary + faq + captions + title + category); redline → `BLOCKED`, daily-check → `NEEDS_HUMAN_REVIEW`. **Gated on the calibration bar (blocker #1).**
- **`PublisherConfig` is EXTENDED** (already exists, config.py:85) with `endpoint`, `keyring_username="publisher"`, `auto_publish_enabled=False`, `timeout`, and a **host-allowlist** (mirroring `LlmConfig.allowed_hosts`). A pydantic validator rejects `auto_publish_enabled=True` with `require_human_approval=True`.
- **Honest opt-in framing (review P1 #6):** enabling auto-publish is **one coupled config decision** (`auto_publish_enabled=true` *forces* `require_human_approval=false`) plus the `--until publish` run flag. Do **not** market this as "triple-gate / defense-in-depth." If genuine defense-in-depth is wanted, add a separate per-run confirmation token (analogous to `--redline-override`) that the config does not imply.
- **Publish endpoint security:** HTTPS-only (validator) + `net_guard.assert_global_ip` (reject private/loopback/metadata) + **host-allowlist** so the destination is constrained to the owner's backend, not "any public HTTPS IP" (review P2 #10 — the POST carries the token + third-party PII). Documented residual: DNS-rebinding/TOCTOU (httpx re-resolves), same accepted residual as the Scrapy path.
- **Publisher token redaction copies the LLM discipline:** keyring (user `publisher`) or `LCP_PUBLISHER_TOKEN`; never in `config.yaml`; never in `minimal_env`. On error: exact-string-replace the token, **never include the backend response body**, route through `redact()` (generic `redact()` misses an opaque/unlabeled token).
- **lcp self-protects against a non-deduping backend (review P1 #8):** before enabling auto-publish, a startup self-check POSTs the same idempotency key twice and refuses to proceed unless the second returns the original — a machine check, not a runbook line. (Or: make retry an explicit operator action, never an automatic edge.)
- **Auto-publish resumes a job at `REVIEW_PENDING` (review P1 #9):** `run --until publish` reads the persisted frozen packet/draft by job_id (like `signoff.approve`'s `draft=None` load), then auto_approve → publish. This composes with the predecessor's persisted-job model.
- **Auto path source quality (review P2 #13):** because no human reviews, the auto path must **not** accept a raw 300-char search-page scrape as publishable. Require article-page sources for auto-publish (search-page sources route to `NEEDS_HUMAN_REVIEW`), or add a coherence/structure check.
- **`NEEDS_HUMAN_REVIEW` backlog actor (review S2):** the re-screen parks daily-check/`AMBIGUOUS_REDLINE` outputs at `NEEDS_HUMAN_REVIEW`, whose only exits need a human the operator turned off. The go/no-go runbook must name an actor + cadence to clear it; decide whether `cleanup` (or a sibling) surfaces it.

## Implementation Units (provisional — finalize after blockers clear)

> Carried from the former Phase 4. Internal order: U1 → U2 → U3(calibration) → U4 → U5 → U6 → U8, with U7 anytime after U1. **U3 (calibration) gates whether the rest may enable.**

- [ ] **Unit 1: State machine surgery** — `AUTO_PUBLISHED` + `PUBLISH_FAILED` + `AUTO_PUBLISHED → SUPERSEDED` retract; `STATE_ALIASES`; reconcile surfaces APPROVED + `.publishing` marker; do NOT add `PUBLISH_FAILED` to `RECONCILABLE_STATES`. Test-first against `tests/test_state_machine.py`. *(Files: `core/state.py`, `pipeline.py`.)*
- [ ] **Unit 2: Publisher adapter + extended `PublisherConfig` + token + host-allowlist** — fail-closed HTTPS POST, `is_global` + host-allowlist on endpoint, exact-string token redaction, no backend body in errors, idempotency self-check. *(Files: `adapters/publisher/backend_publisher.py`, `core/config.py`, `adapters/storage/config_io.py`, `config.example.yaml`.)*
- [ ] **Unit 3 (GATING): Output redline detector calibration + re-screen** — build/annotate a 吃瓜 redline corpus, measure recall, set the enable bar; implement the re-screen over assembled draft text; if keyword recall is below bar, swap in the claim-level NLI detector the codebase anticipates. **Auto-publish may not enable until this passes.** *(Files: `core/rules/risk_rules.py`, `adapters/processor/risk_checker.py`, `spikes/…`.)*
- [ ] **Unit 4: auto_approve (freeze-hash bound) + auto_publish (`.publishing` marker) + `_drive_publish` + `run_until(target=publish)`** — resume from `REVIEW_PENDING`; opt-in validated at the top before any spend; stop-category tagging; fail-closed → `PUBLISH_FAILED`. *(Files: `adapters/publisher/signoff.py`, `pipeline.py`.)*
- [ ] **Unit 5: CLI/GUI publish surface + `--draft`** — mirror invariant; surface stop-category; justify `--draft` vs `--until review` or fold into tests. *(Files: `cli.py`, `gui.py`, `web/{app.js,lex.js}`.)*
- [ ] **Unit 6: `cleanup` extension for the no-human path** — ensure `NEEDS_HUMAN_REVIEW` backlog is surfaced/cleared (not just BLOCKED/DUPLICATE). *(Files: `adapters/publisher/signoff.py`, `cli.py`, `gui.py`.)*
- [ ] **Unit 7: `cleanup` command (BLOCKED/DUPLICATE worklist hygiene)** — carried from predecessor if not already shipped; `BLOCKED/DUPLICATE → SUPERSEDED`, operator-only, redline-override second confirmation. *(Reuses `signoff.supersede`.)*
- [ ] **Unit 8: Publish e2e** — full real chain `… → output re-screen → auto_approve → publisher → AUTO_PUBLISHED`, no shortcut, assert `final_state is AUTO_PUBLISHED`; redline-output and parked jobs receive zero POSTs; non-deduping-backend self-check refuses. *(Files: `tests/test_e2e_pipeline.py`.)*

## Risks & Dependencies (carried)

| Risk | Mitigation |
|------|------------|
| Output re-screen paraphrased past (uncalibrated keyword matcher) | **Blocker #1**: calibration spike + recall bar + NLI fallback (Unit 3) — the gating deliverable. |
| Backend already has a human queue → "no manual step" false | **Blocker #2**: confirm before planning. |
| Backend ignores idempotency key → double-publish | lcp startup self-check (POST key twice, refuse unless deduped); `.publishing` marker; go/no-go gate. |
| Copyright at auto-publish scale | **Blocker #4**: recorded operator acceptance for the auto scale specifically. |
| Endpoint exfiltration (token + PII to wrong public host) | HTTPS + `is_global` + **host-allowlist** (Unit 2). |
| Machine published something wrong | Operator-only `AUTO_PUBLISHED → SUPERSEDED` retract record; backend takedown out-of-band (records, does not un-publish). |
| Image NCII in cover (text screen can't catch) | Out of scope; backend post-hoc review — but on a no-human path this is preventive-gap; note in go/no-go. |
| `NEEDS_HUMAN_REVIEW` parks forever on a "no-human" path | Named actor + cadence in runbook; `cleanup` surfaces it (Unit 6). |

## Sources & References

- **Origin:** [docs/brainstorms/2026-06-22-gossip-pipeline-core-requirements.md](docs/brainstorms/2026-06-22-gossip-pipeline-core-requirements.md)
- **Predecessor (Stages 1–3):** [docs/plans/2026-06-22-001-feat-gossip-pipeline-core-plan.md](docs/plans/2026-06-22-001-feat-gossip-pipeline-core-plan.md)
- Publish-class transition + freeze binding: `src/lcp/adapters/publisher/signoff.py` (`approve` 215-277, `backfill_published_url` 508-599)
- Existing `PublisherConfig`: `src/lcp/core/config.py:85`; keyring/redaction: `config_io.py`, `adapters/llm/client.py:412-426`
- SSRF guard: `src/lcp/adapters/crawler/net_guard.py:211-274`; LLM host-allowlist precedent: `LlmConfig.allowed_hosts`
- Input-only risk gate / faithfulness grounding: `src/lcp/core/rules/{risk_rules,grounding}.py`; gate call `pipeline.py:424`
- Detection-accuracy harness (for calibration): `spikes/detection_accuracy/run_eval.py`
- Prior art: `docs/plans/2026-06-18-001-fix-stabilize-and-harden-pipeline-plan.md` (U8 recovery edge)

## Next Steps

→ Resolve the four "Resolve Before Planning" blockers (calibration spike, backend-queue decision, backend existence, copyright record), then re-enter `/ce:plan` to finalize the provisional units. Until then this plan stays `status: blocked`.
