# Unit 1 spike — detection / grounding accuracy harness

A **runnable measurement harness** for the plan's Phase 0 de-risking spike
(`docs/plans/2026-06-16-001-feat-local-content-processor-mvp-plan.md`, Unit 1).
It scores the **real, deterministic** detectors against a labeled set and prints
a decision table (*strategy × precision/recall × recommended fail-closed
threshold*).

## MVP DECISION (recorded 2026-06-16)

**Grounding strategy for MVP = `substring-only`, fail-closed to human review.
`+NLI/MiniCheck` is deferred (P1) until a real labeled corpus exists.**

Rationale — this is the plan's own *prescribed* branch for the "accuracy
unproven" case (Unit 1 Approach: *"準確度過低/未知 → MVP 退為 advisory-only +
該 reason 一律 route-to-human (fail-closed)"*):

- No real, human-labeled corpus exists yet, so there is **no decision-grade
  measurement** that would justify trusting an automated grounding gate or
  pulling in the heavy NLI/transformer dependency. The numbers above are on the
  synthetic set and validate **mechanics only**.
- The conservative, safe choice is therefore the substring baseline with
  **fail-closed** behaviour: any ungrounded quote/claim → `needs_human_review`
  (reason=grounding), never auto-passed. This is exactly what the shipped code
  does (`grounding.verify_grounding` + `SubstringOverlapStrategy`, fail-closed).
- The `+NLI` path stays a **drop-in seam** (`GROUNDING_STRATEGIES` here, the
  `GroundingStrategy` Protocol in `core/rules/grounding.py`). When a real golden
  set is annotated, run it through this harness (substring vs +NLI head-to-head)
  and revisit. Until then: **do not claim a measured accuracy number.**

risk + dedup gates are likewise advisory + fail-closed (risk redline = hard-stop
BLOCKED; dedup never auto-rejects, only confident-unique passes — R36).

## What this delivers (and what it does NOT)

- ✅ It **runs now**, end to end, with zero extra dependencies, and exits 0.
- ✅ It imports the **production detectors** directly and scores them:
  - grounding — `lcp.core.rules.grounding.verify_grounding` (+ `SubstringOverlapStrategy`)
  - risk — `lcp.core.rules.risk_rules.assess_risk`
  - dedup — `lcp.core.rules.dedup_rules.assess_dedup`
  - The company LLM is **not** needed: these are pure and deterministic.
- ✅ It leaves a **clearly-marked seam** (`GROUNDING_STRATEGIES` in `run_eval.py`)
  where a `+NLI` grounding strategy plugs in later, behind the same
  `GroundingStrategy` Protocol — **no heavy ML dependency is added here**.
- ❌ It does **NOT make the accuracy DECISION**. Whether grounding should stay
  *substring-only* or go *+NLI/MiniCheck* is a real engineering decision that
  needs (1) a **real labeled corpus** and (2) for NLI, an **actual model**.
  The numbers printed against the bundled sample validate **mechanics only**.

> **Honest framing.** The bundled `sample_labeled.jsonl` is small, synthetic, and
> made of neutral invented events. Good numbers on it prove the wiring works —
> they are **not** evidence that the substring baseline is good enough for
> production. That call waits on the real golden set below.

## How to run

```sh
# from the repo root
./.venv/bin/python spikes/detection_accuracy/run_eval.py

# machine-readable metrics instead of the table
./.venv/bin/python spikes/detection_accuracy/run_eval.py --json

# point it at your own labeled set (e.g. the real golden set)
./.venv/bin/python spikes/detection_accuracy/run_eval.py --labeled spikes/detection_accuracy/golden_set/labeled.jsonl

# ALSO score the opt-in +NLI LLM entailment judge alongside the substring
# baselines (needs an LLM endpoint configured; one network call per claim):
./.venv/bin/python spikes/detection_accuracy/run_eval.py --with-nli
./.venv/bin/python spikes/detection_accuracy/run_eval.py --with-nli --config config.yaml --labeled spikes/detection_accuracy/golden_set/labeled.jsonl
```

### The `+NLI` LLM judge (`--with-nli`) — opt-in, now real

The `+NLI` path the plan reserved is implemented as
`lcp.adapters.llm.nli_grounding.LlmGroundingStrategy`: it uses the company
OpenAI-compatible LLM as a claim-level entailment judge (one constrained, tiny
YES/NO call per claim) and satisfies the same `GroundingStrategy` Protocol as the
substring baseline, so the harness scores them **head to head** on the same set.

- **Default run does NOT use it** — it stays offline + zero-dependency. Pass
  `--with-nli` to enable it (and have `llm.base_url` + the api_key configured).
- **Security:** the judge is zero-capability (returns one word), and both the
  source and the claim are sanitized + datamarked as DATA — same lethal-trifecta
  posture as the assembler. It resolves no URL.
- **Fail-closed:** anything but a confident `YES` (incl. a truncated/empty answer
  or an LLM error) → "not grounded" → routed to a human.
- **Still not the decision.** A good `+NLI` number on the *synthetic* set proves
  the path works end to end (it has been run live against `gemma4-31b-heretic`);
  choosing substring-only vs +NLI for production still requires the real labeled
  corpus below. MVP default remains substring-only (see the MVP DECISION above).

Reading the table:

- **positive class = the unsafe outcome the fail-closed gate must catch.**
  - grounding: positive = `needs_human_review` (claim/quote not grounded)
  - risk `flag_any`: positive = `blocked` or `needs_human_review` (not clean)
  - risk `block_only`: positive = `blocked` (hard-stop tier precision)
  - dedup `not_unique`: positive = `duplicate` or `uncertain` (R36: only a
    *confident* `unique` is allowed to pass)
- **FN (false negative)** = an unsafe item wrongly cleared. This is the
  dangerous error. The recommendation column says **FAIL-CLOSED** whenever
  recall < 1.0 — route that reason to a human instead of auto-passing.

## The labeled-set format

One JSON object per line (`.jsonl`). Common fields: `id`, `kind`
(`grounding` | `risk` | `dedup`), and an optional `note`.

**grounding** — does the draft's claims/quotes hold up against the source?
```json
{"id": "gnd-001", "kind": "grounding",
 "source_text": "....",
 "draft": {"event_body": "....",
           "quotes": [{"text": "verbatim span that must be in the source"}],
           "faq": [{"question": "...", "answer": "...."}]},
 "label": "grounded" | "ungrounded"}
```

**risk** — defamation / privacy / redline gate.
```json
{"id": "risk-001", "kind": "risk",
 "title": "....", "body": "....",
 "has_source": true, "contains_serious_claim": false,
 "label": "pass" | "needs_human_review" | "blocked"}
```

**dedup** — duplicate against an in-memory index.
```json
{"id": "dedup-001", "kind": "dedup",
 "title": "....", "body": "....",
 "site_index_available": true,
 "index": [{"job_id": "j100", "title": "....", "body": "...."}],
 "label": "unique" | "duplicate" | "uncertain"}
```

## How to add a REAL golden set

The plan calls for **30–60 representative samples**, human-annotated for
defamation / privacy / grounding, to actually calibrate thresholds and decide
grounding strength.

1. Put the file under **`spikes/detection_accuracy/golden_set/`** (e.g.
   `golden_set/labeled.jsonl`), using the same line format as above.
2. Run: `./.venv/bin/python spikes/detection_accuracy/run_eval.py --labeled spikes/detection_accuracy/golden_set/labeled.jsonl`
3. To trial a `+NLI` strategy, register it in `GROUNDING_STRATEGIES` inside
   `run_eval.py` (the seam is commented there). It must satisfy the
   `GroundingStrategy` Protocol — and it is scored **alongside** the substring
   baseline, so you compare them head to head on the same corpus.

### ⚠️ The golden set is treated as PII — it is gitignored

Real samples come from real sources and **must be treated as PII** (plan R44).
`golden_set/` (and `data/`) under `spikes/` are in the repo `.gitignore` and
**MUST NOT be committed**:

```
spikes/**/golden_set/
spikes/**/data/
```

Only `sample_labeled.jsonl` (this synthetic, non-PII, no-real-people set),
`run_eval.py`, and this `README.md` are committed. De-identify anything you put
in `golden_set/` and keep it local.

## Files

- `run_eval.py` — the harness (imports the real detectors, computes
  precision / recall / FP / FN, prints the decision table; NLI seam marked).
- `sample_labeled.jsonl` — small synthetic, non-PII labeled set (committed).
- `golden_set/` — your real, de-identified golden set (**gitignored**).
- `../../tests/test_spike_eval.py` — harness-validation test so this can't
  silently rot.
