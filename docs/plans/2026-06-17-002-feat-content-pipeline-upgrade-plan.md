---
title: "feat: Content Pipeline Upgrade — watermark / cover / AI copy templates / ingest"
type: feat
status: active
date: 2026-06-17
origin: docs/brainstorms/2026-06-17-content-pipeline-upgrade-requirements.md
---

# feat: Content Pipeline Upgrade

## Overview

Add four SOP-driven capabilities to `lcp` (Eatmelon), the compliance-first local content pipeline, **without leaving the existing compliance envelope**: (A) watermark — ADD an official watermark to body images + cover, and REMOVE watermarks **only on owned/licensed assets** behind a segregation-of-duties attestation; (B) AI copy — keep R16 constrained-rewrite, add per-栏目 prompt-template management + AI-generated captions/FAQ/subheads; (C) cover — keep the existing 1300×640 collage, add official watermark + an advisory safe-area check; (D) crawl/ingest — keep public-source + local material-pack import only, improve mixed-folder ingest.

Three of the four already have foundations (Stage-1 crawl, cover collage in `normalizer.py`, constrained-rewrite LLM). Only watermark add/remove is net-new. Delivery is phased: **Batch 1 = copy + cover** (highest daily value, lowest risk), **Batch 2 = de-watermark** (spike-gated, may be cut), **Batch 3 = ingest** (smallest).

## Problem Frame

A non-technical operator runs a 吃瓜/爆料 content site. `lcp` already runs `crawl → normalize → risk/dedup → constrained-rewrite draft → review packet`. Against the operator's real SOP (`docs/spec/远程内容编辑发布流程SOP_新媒体版.md`), the most time-consuming manual steps — copywriting and cover-making — are still by hand, and watermark handling isn't built at all. The goal: drop per-article manual work to "human only reviews," **without compliance/quality collapsing at scale** — where the real exposure is republication-of-source-defamation, copyright, and image PII. (see origin: `docs/brainstorms/2026-06-17-content-pipeline-upgrade-requirements.md`)

## Requirements Trace

- **UR1** Watermark-ADD primitive (Pillow) for body images + cover; brand mark, not authorship; source retained in audit/source field.
- **UR2/UR3** De-watermark default-locked; unlock requires (a) verifiable license-evidence, (b) **independent reviewer ≠ submitter** approval, (c) full audit; honest "attestation not authentication" disclosure. Bounded **exception to (amendment of) R2**, not "R2 unchanged."
- **UR4** De-watermark provenance (`watermark_removed=true` + evidence ref) in manifest; failure/low-confidence → `needs_revision`, never silent partial output; EXIF/image-PII stripped on output.
- **UR5** Keep R16 constrained-rewrite; narrative bound to source; machine output `needs_human_review`.
- **UR6/UR8** Per-栏目 prompt-template management (config-overrides-first); templates are a **checked object** — injection/jailbreak lint, cannot rewrite system constraints.
- **UR7** AI generates low-risk structural pieces (captions/FAQ/subheads/title candidates) — **net-new generation** requiring a grounding contract + freeze-binding extension.
- **UR9/UR10** Cover safe-area advisory check + official watermark; reject text / 3rd-party watermark / links / black-white borders (geometry auto; aesthetic soft; OCR-class checks advisory only).
- **UR11/UR12** Keep Scrapy public-source + local ingest only (no JS/login-wall, no anti-bot bypass); improve mixed-folder material-pack ingest + completeness check.
- **UR13–UR16** All new actions audited (PII-free); idempotent + dry-run-safe; **no auto-publish**; GUI/CLI 1:1 with the operator surface.

**Success criteria carried forward (origin):** per-article human review-touch/time DROPS vs an `lcp.db` baseline (and must NOT rise from added AI artifacts); R4/R5 defamation/未证实 detection gets a measurable 漏检率 acceptance bar; de-watermark has measurable removable-type / residual / fallback bars on a labeled set; everything stays no-auto-publish + audited + honest about residual limits.

## Scope Boundaries

- **No** de-watermarking of third-party/unlicensed assets (owned/licensed + independent-review gate only).
- **No** free full-text generation (R16 stays; AI only structural pieces).
- **No** 微博/小红书/抖音/知乎 (JS/login-wall) crawling; no login/paywall/anti-bot bypass; no Playwright.
- **No** auto-draft / auto-publish (MVP Stage 5/6 boundary stands).
- **No** AI cover image generation; cover is compose + watermark + advisory checks only.
- **No** torch/opencv/mediapipe in the cover-composition path; de-watermark deep deps stay **isolated** from the main venv (see Key Decisions).

## Context & Research

### Relevant Code and Patterns

- **Media:** `src/lcp/adapters/media/normalizer.py` — `normalize_image` (`exif_transpose → thumbnail(LANCZOS) → save JPEG q90`), `make_cover`/`_cover_cells` (1–4 image 1300×640 via `ImageOps.fit`), Laplacian-variance blur, `MAX_IMAGE_PIXELS` decompression-bomb guard (Pillow-only, no numpy/opencv). Watermark-ADD primitive lives here.
- **Media gate:** `src/lcp/adapters/processor/media_checker.py` — `run_media_gate`/`_validate_images`; cover is composed **from already-normalized body images in one pass**; `DependencyError` on missing ffmpeg (mirror for a missing inpaint engine). Pure thresholds in `src/lcp/core/rules/asset_rules.py` (safe-area geometry goes here).
- **LLM:** `src/lcp/adapters/llm/assembler.py` — hardcoded `build_system_prompt` (constant; emits 标题/引言/一分钟快速看懂/事件经过/FAQ/结尾 — **no captions/subheads today**), datamarking, finish_reason gate; `client.py` `LlmClient` (zero-capability, `client_factory` injection seam, dry_run).
- **Grounding/lint:** `src/lcp/core/rules/grounding.py` — `_split_claims` grounds **only** `event_body` sentences + `faq[*].answer` (NOT quotes, NOT captions); `is_grounded("")` returns True (empty claim auto-passes). `src/lcp/core/rules/lint_rules.py` — injection-feature checks (template linter reuses). The canonical frozen body text is assembled in `review_packet.py` `_draft_body_text()` (signoff re-derives via `compute_body_sha256`).
- **Sign-off / freeze:** `src/lcp/adapters/publisher/signoff.py` — `--attest`/`backfill`, `DISCLAIMER`, reviewer-whitelist, `approve()` body-hash check (≈L196-202); `review_packet.py` `_draft_body_text()` feeds `body_sha256` = `intro+event_body+summary+quick_facts+faq` — **NOT captions/image_sections**. **Correction:** the **cover IS already frozen** via a separate `cover_sha256` (`_sha256_file(cover_path)` + `bound_cover_sha256`), so watermark must run **before** packet freeze (it does — watermark is in the pre-packet media gate; do NOT re-watermark after freeze). `build_review_packet()` takes `actor: str = "human"` (a caller-passed string, not an OS identity) while `approve()` uses `observed_os_user()` — **two different identity namespaces** — and **nothing enforces approver ≠ submitter** today.
- **Isolation:** `src/lcp/adapters/crawler/crawl_runner.py` — subprocess-per-job, scrubbed `minimal_env` (the pattern the de-watermark engine subprocess mirrors); `ingest.py` (local material import).
- **Models/config:** `core/models.py` (`AssetRef`: kind/path/source_url/sha256/state/note; `Manifest` PII-free), `core/draft.py` (`Draft`, `MediaSection.caption`, `FaqItem`), `core/config.py` (`MediaConfig`, `LlmConfig`, `categories`; add `watermark:`/`templates:`/`inpaint:` Pydantic blocks), `core/state.py` (JobState machine).
- **GUI:** `gui.py` `Api` (+`disclaimer()`); `web/lex.js` `STATE_ACTIONS` (JobState→actions, **empty=fail-closed**) + `LEX.honesty`; `web/app.js` `renderActions`≈L694-715 / `buildActionRow` / `reviewerSelect`≈L920 / backfill checkbox call ≈L790 / `POLL_MS=1500` `POLL_CAP=120` (~90s); strict CSP, all text via `textContent`.
- **Tests:** call `main(argv)` directly (no `CliRunner`), assert exit code + `capsys`; inline `@pytest.fixture` (store/audit in `test_pipeline_batch.py`); helpers `_setup()`/`_api()`/`_sharp_jpeg()`; GUI parity via importing `_processed_job_with_draft` from `tests/test_cli_skeleton.py`; fake LLM via `client_factory` (`tests/llm/test_client.py`).
- **Packaging:** `pyproject.toml` extras `crawl/media/llm/dedup/gui/dev`; mypy strict on `core/`+`adapters/`.

### Institutional Learnings

- `docs/solutions/` and `docs/learnings/` **do not exist** — no prior-solution corpus. Institutional memory is `docs/plans/` (esp. `2026-06-16-001-feat-local-content-processor-mvp-plan.md` Units 4/5/7), `docs/security/pii-inventory.md`, and the now-committed `docs/spec/远程内容编辑发布流程SOP_新媒体版.md`.
- **Existing watermark engine `static-ghost`** (`redredchen01/static-ghost` v0.4.2; also a local skill) does fixed-logo / semi-transparent / full-page / **video** removal via `detect/remove/batch/process` (LaMa + OpenCV + FFmpeg). It is a **candidate engine for Batch 2**, but uses torch + first-run HuggingFace weight download (offline-hostile) and is video-oriented — so it is one option the spike weighs, not a foregone choice.
- MVP-plan Pillow gotchas to honor: decompression-bomb warning→error, **never** `MAX_IMAGE_PIXELS=None`, use `Resampling.LANCZOS`.
- `pii-inventory.md` records a **known residual**: pin-IP-at-connect is not wired on the Scrapy path (DNS-rebinding/TOCTOU). If UR11 widens the allowlist, re-state this; do not assume SSRF is fully closed.

### External References

- **De-watermark (decision-changing):** torch is **not** required — ONNX route (`onnxruntime`, tens of MB) runs LaMa/MI-GAN. **MI-GAN ONNX ≈ 29.5 MB (MIT)** is small enough to bundle offline; LaMa-ONNX 208 MB (Apache-2.0) side-load. `simple-lama-inpainting` (0.1.2, 2023) pins `pillow>=9.5,<10`+`numpy<2`; `IOPaint` pins `Pillow==9.5.0` — **both incompatible with the repo's Pillow 12 → cannot live in the main venv**. `cv2.inpaint` (no torch, instant) is weak on block/semi-transparent watermarks. Ref arch `remove-ai-watermarks` (cv2 default + LaMa-ONNX optional).
- **Pillow watermarking:** `alpha_composite` needs RGBA; **JPEG has no alpha → `convert("RGB")` before save**; `exif_transpose` before watermarking; text via `ImageDraw.text(anchor=…)` + truetype. EXIF/PII: don't pass `exif=` on save (or `del exif[0x8825]` GPS IFD); `convert("RGB")`→JPEG drops EXIF naturally.
- **Prompt-injection:** layered hardcoded shell (templates never touch SYSTEM) + restricted-variable allowlist via `str.format_map` (**not** Jinja2 — its sandbox is anti-RCE not anti-injection, has escape CVEs) + datamark + template linter (reject unknown placeholders / role markers / datamark tokens / fences / zero-width / bidi / homoglyph(NFKC) / length>2–4KB; warn on injection phrases; lint on save AND import). No LLM-judge (no tools → blast radius is "low-quality text"; deterministic canary + schema instead).
- **Cover heuristics:** composition is known at compose time → "subject outside safe area" is **arithmetic** (safe box `(130,64,1170,576)` at 10% margin), auto-warn; border/letterbox via `ImageStat` on 8px strips (mean≤24/std≤8, use stddev not extrema); top-heavy via `FIND_EDGES` upper/lower ratio>1.6 (soft); crowding via entropy/edge-density (soft, never block); draw safe-area box on the preview for human judgment. Pillow-only (+numpy optional for saliency); **never** opencv/torch here.

## Key Technical Decisions

- **Watermark-ADD is a shared Pillow primitive, applied as a final pass.** Body images are watermarked from clean normalized copies; the **cover is composed from clean (un-watermarked) tiles and then watermarked once** — this resolves the "cover inherits per-tile marks vs single cover mark" conflict. Two watermark-asset sizes (body 800px vs cover 1300×640). Convert RGB before JPEG; watermark after `exif_transpose`.
- **De-watermark deps stay isolated from the main venv, and engine choice is spike-decided.** The main `[media]` extra never gains torch/opencv. A separate `[inpaint]` extra (or external `static-ghost` subprocess) runs in its own environment, invoked like the Scrapy `crawl_runner` subprocess (scrubbed env, per-job). The spike chooses between (a) bundled **MI-GAN-ONNX (no torch, offline)** and (b) reusing **static-ghost (torch, video-capable, exists)**, on accuracy AND CPU latency.
- **De-watermark masks come from a config fixed-box or an operator-drawn box (human-in-the-loop), not auto-detection in v1.** Large/floating/tiled watermarks are explicitly out-of-scope v1.
- **Segregation of duties is net-new plumbing.** No submitter/approver split exists. Record the submitting actor at process/create time (audit); the de-watermark attestation must be approved by a whitelisted reviewer ≠ that submitter, with a verifiable license-evidence field and a `DEWATERMARK_DISCLAIMER` honesty callout.
- **R2 is amended, not "unchanged."** The de-watermark exception is a bounded, re-ratified amendment to R2's absolute prohibition (origin doc honest framing).
- **AI structural copy needs a grounding contract AND a freeze-binding extension — scoped to captions/subheads/image_sections.** FAQ already has a grounded path (`_split_claims` covers `faq[*].answer`, though the assembler emits `faq=[]` today, so FAQ generation reuses that path); the net-new grounding + freeze work is specifically for captions/subheads/image_sections. Extend `review_packet.py` `_draft_body_text()` to include them, or post-freeze caption edits go undetected. **Caveat:** a caption summarizing an image often has no verbatim source span, so binary grounded-vs-flagged can collapse to always-flagged — which RAISES review burden; captions that can't ground are operator-hints requiring human confirmation, not auto-pass nor hard-block.
- **Templates render outside the SYSTEM constant via `str.format_map` allowlist; slot VALUES are datamarked.** The allowlist bounds slot KEYS only — slot VALUES come from scraped source and can carry injection, so they get datamarked like source text. `LlmClient.chat` has only system+user today → template renders into a delimited USER sub-block (or a new `client.py` developer message — Unit 3 decides). Unknown placeholders rejected at save; templates linted on save + import.
- **Cover checks: geometry auto-warns, aesthetics soft-suggest, OCR-class checks are advisory — this DEMOTES UR10's hard-gate.** UR10 originally required `needs_revision` on cover text/3rd-party-watermark/links; that is not feasibly automatable on the Pillow-only stack, so it becomes **advisory + human preview**. This is an explicit **amendment to UR10** (recorded in Requirements Trace), not a silent reinterpretation. Black/white-border + safe-area geometry remain auto-warnings; aesthetic (top-heavy/busyness) stay soft.

## Open Questions

### Resolved During Planning
- **Inpainting dependency conflict** → isolate from main venv (Pillow 12 vs `pillow<10` pins); ONNX (no torch) preferred for offline footprint.
- **Watermark vs cover ordering** → cover composes from clean tiles, single watermark pass after compose.
- **Prompt-template engine** → `str.format_map` allowlist, not Jinja2.
- **Cover "subject in safe area"** → compose-time arithmetic, not CV.
- **SOP availability** → committed to `docs/spec/` (pre-planning task done).

### Deferred to Implementation
- **De-watermark go/no-go + engine choice** → the Batch-2 spike (Unit 6) decides build/cut, engine (MI-GAN-ONNX vs static-ghost), and acceptance bars, measured on the **operator's actual laptop** (CPU latency has no trustworthy public data).
- Exact watermark-asset artifacts (transparent PNG/font) and per-resolution placement constants.
- Final safe-area/top-heavy/border thresholds (start: safe box 10%, border mean≤24/std≤8, top-heavy ratio>1.6) — calibrate on the operator's own sample.
- Per-asset vs per-job granularity for attestation + de-watermark results (mixed-ownership packs).
- Exact `str.format_map` allowlisted variable set per 栏目 template slot.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

**Watermark ordering inside the media gate (resolves the cover conflict):**
```
normalize_image(asset)  ──►  clean_body_800px ──┐
                                                 ├─► make_cover(clean tiles) ─► cover_1300x640 ─► add_watermark(cover-size)  ─► cover.jpg
                                                 └─► add_watermark(body-size) per image ─────────────────────────────────────► images/*.jpg
   [optional, pre-normalize, gated]  de_watermark(asset) ──► (isolated subprocess) ──► cleaned asset ──► (re-enters normalize)
```

**Layered prompt — NOTE: `LlmClient.chat` today has only `system`+`user` (no developer role).** Two viable shapes (Unit 3 decides):
```
SYSTEM  (hardcoded constant; zero-capability + grounding + anti-injection)   ← templates NEVER here
USER    [ 栏目 template via str.format_map({allowlisted slots}), own delimited block,
          treated as untrusted; slot VALUES datamarked (they come from scraped source) ]
        + [ datamarked source text ] + grounding restated after the source
```
Alternative: add a second hardcoded developer/system-tier message in `client.py` (then list `client.py` in Unit 3 Files). Either way the template lands OUTSIDE the SYSTEM constant, and the allowlist bounds slot KEYS, not VALUES — so VALUES are datamarked too.

**De-watermark trust/flow (net-new SoD + isolation):**
```mermaid
graph TB
  A[Operator: request de-watermark on job] --> B{Attestation gate (UR2)}
  B -->|license evidence + reviewer != submitter| C[audit: attestation event]
  B -->|missing any| X[locked: no de-watermark]
  C --> D[isolated inpaint subprocess (config/operator mask)]
  D -->|ok| E[EXIF-stripped cleaned asset + manifest watermark_removed]
  D -->|fail / low-confidence| F[NEEDS_REVISION: no silent partial]
  E --> G[normal media gate + human review]
```

## Implementation Units

```mermaid
graph TB
  U1[U1 watermark-ADD primitive] --> U2[U2 cover watermark + safe-area]
  U1 --> U5[U5 batch-1 GUI/CLI]
  U3[U3 template mgmt + linter] --> U4[U4 AI captions + grounding + freeze]
  U2 --> U5
  U3 --> U5
  U4 --> U5
  U6[U6 de-watermark SPIKE go/no-go] --> U7[U7 SoD attestation plumbing]
  U6 --> U8[U8 de-watermark integration]
  U7 --> U8
  U8 --> U9[U9 attestation + inpaint GUI]
  U10[U10 mixed-folder ingest]
```

### Phase / Batch 1 — Copy + Cover (low-risk, highest daily value)

- [ ] **Unit 1: Watermark-ADD Pillow primitive**

**Goal:** A shared `add_watermark(image, kind)` primitive (logo + text modes, corner anchor, opacity, margin) reused by body images and cover.

**Requirements:** UR1, UR13, UR14

**Dependencies:** None

**Files:**
- Create: `src/lcp/adapters/media/watermark.py`
- Modify: `src/lcp/adapters/media/normalizer.py` (call after normalize/compose), `src/lcp/core/config.py` (add `WatermarkConfig`: enabled, mode logo|text, asset paths per size, position, opacity, margin)
- Test: `tests/media/test_watermark.py`

**Approach:**
- RGBA `alpha_composite` for logo; `ImageDraw.text(anchor=…)` + truetype for text; **`convert("RGB")` before JPEG save**; run after `exif_transpose`. Two asset sizes (body 800px / cover 1300×640). Honor existing decompression-bomb guard; never reintroduce `MAX_IMAGE_PIXELS=None`.
- Brand-mark only — does not alter source provenance fields; dry-run does not write watermarked output.

**Patterns to follow:** `normalizer.normalize_image` pure-ish transform + IO separation; `MediaConfig` Pydantic shape.

**Test scenarios:**
- Happy path: logo watermark composited at bottom-right with margin on an 800px body image → output is RGB JPEG, watermark pixels present at expected corner.
- Happy path: text watermark with truetype font + opacity → semi-transparent text rendered at anchor.
- Edge case: RGBA source → saved JPEG is RGB (no "cannot write mode RGBA" error).
- Edge case: EXIF-rotated source → watermark lands correctly after `exif_transpose`.
- Edge case: watermark larger than image / zero margin → clamped, no crash.
- Error path: missing/corrupt watermark asset → typed error, gate marks asset, no silent skip.
- Integration: dry-run → no watermarked file written.

**Verification:** Body + cover outputs carry the official mark at configured position; JPEGs are valid RGB; dry-run produces none.

- [ ] **Unit 2: Cover watermark + safe-area advisory**

**Goal:** Watermark the cover once (via U1) and add compose-time safe-area geometry + Pillow aesthetic heuristics with a preview overlay.

**Requirements:** UR9, UR10

**Dependencies:** Unit 1

**Files:**
- Modify: `src/lcp/adapters/media/normalizer.py` (`make_cover`: capture each tile's placement rect; single watermark pass after compose), `src/lcp/core/rules/asset_rules.py` (pure safe-area/border/top-heavy/busyness decisions), `src/lcp/adapters/processor/media_checker.py` (surface advisories)
- Create: `src/lcp/adapters/media/cover_checks.py` (Pillow `ImageStat`/`FIND_EDGES`/`entropy` measurements feeding pure rules), preview safe-area box drawing
- Test: `tests/media/test_cover_checks.py`, `tests/core/test_asset_rules_safearea.py`

**Approach:**
- Safe box `(130,64,1170,576)`; subject-rect-vs-safe-box is arithmetic from compose placement → **auto-warn**. Border/letterbox via 8px strip `ImageStat` (mean≤24/std≤8) → **auto-warn**. Top-heavy (`FIND_EDGES` ratio>1.6) + busyness (entropy/edge-density) → **soft suggestion, never block**. Text/3rd-party-watermark/links in cover → **advisory only** (not feasibly automatable on Pillow-only) + human preview. Draw the safe-area box on the preview image.
- Thresholds are config-overridable starting points (calibrate later).

**Patterns to follow:** pure decisions in `core/rules/asset_rules.py`, measurements in adapter; advisory outcomes flow like existing media-gate notes.

**Test scenarios:**
- Happy path: 3-image collage, subject within safe box → no warning.
- Edge case: subject rect crosses `(130,64,1170,576)` → safe-area auto-warning with which tile.
- Edge case: 8px black bar on one side → border warning; near-black JPEG noise within tolerance → no false positive.
- Edge case: top-heavy composite (edge-energy ratio>1.6) → soft suggestion (not `needs_revision`).
- Edge case: busy/dense image → "busyness" soft note, never blocks.
- Integration: preview image returned with safe-area box drawn; advisory list attaches to the cover report.

**Verification:** Cover carries one watermark; geometry warnings are deterministic; aesthetic notes are advisory-only; preview shows the safe box.

- [ ] **Unit 3: Prompt-template management + template linter**

**Goal:** Per-栏目 templates (config-overrides-first) rendered into a hardcoded shell via `str.format_map` allowlist, with a linter treating templates as a checked object.

**Requirements:** UR6, UR8, UR5

**Dependencies:** None

**Files:**
- Create: `src/lcp/adapters/llm/templates.py` (load per-栏目 templates from config, `str.format_map` allowlist render), `src/lcp/core/rules/template_lint.py` (pure linter)
- Modify: `src/lcp/adapters/llm/assembler.py` (layered SYSTEM/DEVELOPER/USER; render template into DEVELOPER task slot, never SYSTEM), `src/lcp/core/config.py` (`templates:` block per category)
- Test: `tests/llm/test_templates.py`, `tests/core/test_template_lint.py`

**Approach:**
- Allowlisted named slots only (`{title}`, `{summary}`, …); reject unknown placeholders / field names containing `.`/`[`/`!` at save. **No Jinja2.** Hardcoded SYSTEM keeps zero-capability + grounding + anti-injection; template cannot reach it.
- Linter (reuse `lint_rules.py` injection features): hard-reject unknown placeholders, datamark tokens, role markers, code fences, zero-width/bidi/homoglyph (NFKC), length>~4KB; warn on injection phrases; **run on save and on import** of shared templates.

**Execution note:** Add a failing linter test for a malicious template (e.g., one embedding a datamark token / "ignore previous") before wiring render.

**Patterns to follow:** `assembler.build_system_prompt` constant + datamarking; `lint_rules.py` feature checks.

**Test scenarios:**
- Happy path: valid 网红黑料 template with allowlisted slots renders into the DEVELOPER slot; SYSTEM constant unchanged.
- Edge case: template with unknown placeholder `{evil}` → rejected at save.
- Error path: template embedding the datamark delimiter / a role marker / a code fence → linter hard-reject.
- Error path: zero-width / bidi / homoglyph after NFKC → reject; >4KB → reject.
- Error path: template containing "ignore previous instructions" → warn (saveable, flagged).
- Integration: rendered prompt never places template text in the SYSTEM message (assert message roles).

**Verification:** Operators select/edit per-栏目 templates; malicious templates can't reach SYSTEM or rewrite constraints; lint runs on save + import.

- [ ] **Unit 4: AI captions / FAQ / subheads + grounding contract + freeze-binding extension**

**Goal:** Generate the net-new structural pieces, give them a grounding contract, and extend the freeze hash so reviewed AI content can't be silently edited after freeze.

**Requirements:** UR5, UR7

**Dependencies:** Unit 3

**Files:**
- Create: `src/lcp/adapters/llm/copywriter.py` (generate captions/FAQ/subheads/title candidates; `needs_human_review=True`; dry-run safe)
- Modify: `src/lcp/core/rules/grounding.py` (`_split_claims` extend to caption/subhead claims, or add a dedicated caption-grounding check), `src/lcp/adapters/publisher/signoff.py` (extend frozen `body_sha256` to cover captions/image-sections), `src/lcp/core/draft.py` (populate `MediaSection.caption`), `src/lcp/adapters/llm/assembler.py` (invoke after assemble, before lint)
- Test: `tests/llm/test_copywriter.py`, `tests/core/test_grounding_captions.py`, `tests/test_freeze_binding.py`

**Approach:**
- Captions/subheads are **new content** (not generated today) → either bind to source sentences (grounding) or mark clearly as non-grounded operator-editable hints requiring human confirmation; default = grounded + `needs_human_review`. Honor SOP 第七章 de-dup (no mechanical reuse of subheads/FAQ).
- **Freeze binding:** include captions/image-section text in `body_sha256` so `approve()` refuses a post-freeze caption edit. This closes a silent integrity hole.
- Net effect must be a per-article review-touch DROP (origin success criterion) — keep generation scope tight; if review load rises, narrow it.

**Execution note:** Start with a failing test asserting a post-freeze caption edit is rejected by `approve()`.

**Patterns to follow:** `assembler` zero-capability + dry-run; `client_factory` stub injection in tests; existing `grounding._split_claims` claim shape.

**Test scenarios:**
- Happy path: captions/FAQ/subheads generated, all `needs_human_review=True`, dry-run calls no LLM.
- Edge case: empty/insufficient source for a caption → no fabricated caption; flagged for human.
- Error path: LLM truncated/empty (finish_reason≠stop) → `needs_revision` (existing contract honored).
- Integration (grounding): a caption not supported by source claims → routed to human review, not silently passed.
- Integration (freeze): edit a caption after `review-packet` freeze → `approve()` body-hash mismatch refusal.
- Integration (de-dup): two articles' subheads/FAQ are not mechanically identical.

**Verification:** Captions/FAQ/subheads exist + marked for review + grounded/flagged; post-freeze edits caught; dry-run inert.

- [ ] **Unit 5: Batch-1 GUI/CLI surface**

**Goal:** Expose watermark toggle, 栏目 template picker, and cover preview + safe-area warnings via the existing operator surface, 1:1 CLI/GUI.

**Requirements:** UR16

**Dependencies:** Units 1–4

**Files:**
- Modify: `src/lcp/cli.py` (flags: `--watermark/--no-watermark`, `--template <栏目>`), `src/lcp/gui.py` (`Api.templates()`, watermark/template params on process), `src/lcp/web/app.js` (`buildActionRow` branches, template `<select>` via `reviewerSelect` pattern, cover preview + safe-area note render), `src/lcp/web/lex.js` (`STATE_ACTIONS` entries + labels), `src/lcp/web/app.css`, `src/lcp/web/index.html` (cover-preview slot in `view-job`)
- Test: `tests/test_cli_watermark_template.py`, `tests/test_gui_batch1.py`

**Approach:**
- Watermark on/off + template pick are **process-time inputs** (chosen before the LLM/media run) with optional Setup defaults; surfaced in the create/process panel. Cover preview + safe-area advisory render on the job packet card. Checkbox→bool and dropdown←list mirror the existing `--attest`/`reviewerSelect` wiring. Strict CSP; all text via `textContent`.

**Patterns to follow:** `app.js` `renderActions`/`buildActionRow`/`reviewerSelect`; `gui.py` `Api` method shape; CLI `main(argv)` test style.

**Test scenarios:**
- Happy path (CLI): `process --template 网红黑料 --watermark` applies template + watermark; `--no-watermark` skips.
- Happy path (GUI): `Api.templates()` populates the dropdown; selecting one drives generation.
- Edge case: no templates configured → dropdown empty-state, not a dead end.
- Integration (parity): GUI process with watermark+template reaches the same PROCESSED state as the CLI (reuse `_processed_job_with_draft`).
- Integration: cover preview renders with safe-area box + advisory text via `textContent`.

**Verification:** Operator can pick template + toggle watermark + see cover preview/warnings in GUI and CLI identically.

### Phase / Batch 2 — De-watermark (spike-gated; may be cut)

- [ ] **Unit 6: De-watermark accuracy + latency SPIKE (go/no-go)**

**Goal:** Decide BUILD-or-CUT and engine choice with measured data on the operator's actual laptop. **This gates all of Batch 2.**

**Requirements:** UR2–UR4 (gate)

**Dependencies:** None (run first in Batch 2)

**Files:**
- Create: `spikes/dewatermark/run_eval.py` (stratified harness, prints accuracy + wall-clock table; mirrors `spikes/detection_accuracy/`)
- Test: `tests/spikes/test_dewatermark_harness.py` (harness mechanics only)

**Approach:**
- 30–50 owned/licensed samples stratified into 5 buckets: (a) thin logo on smooth bg, (b) thin logo on texture, (c) semi-transparent overlay, (d) large/tiled/floating, (e) over face/subject. Metrics: SSIM/PSNR vs hand-cleaned + **human publishable-rate per bucket** (the real gate) + **wall-clock per image on the target laptop** (latency is the single biggest unknown). Compare engines: **MI-GAN-ONNX (no torch, ~29.5 MB, bundleable)** vs **static-ghost (torch, exists, video-capable)**; cv2.inpaint as a cheap baseline. Confirm static-ghost licensing/maintenance if chosen.
- Output a go/no-go: per-bucket acceptance bars (e.g. (a)(c) ≥90%, (b)(e) ≥70%, (d) out-of-scope), chosen engine, and latency verdict (if only LaMa passes quality but is too slow → degrade UX to batch/background).

**Execution note:** This is a measurement spike, not a feature — no production wiring; it informs the Unit 7/8 go/no-go.

**Test scenarios:** `Test expectation: harness-only` — assert the eval script loads samples, runs an engine, and emits the metric table; **no production behavior**.

**Verification:** A decision table (engine, per-bucket publishable-rate, residual, latency) exists; the team can say BUILD (which engine) or CUT with evidence.

- [ ] **Unit 7: Segregation-of-duties attestation plumbing**

**Goal:** Net-new submitter identity + independent-reviewer de-watermark attestation + verifiable evidence + audit + honest disclosure.

**Requirements:** UR2, UR3, UR13

**Dependencies:** Unit 6 = GO

**Files:**
- Modify: `src/lcp/adapters/publisher/signoff.py` (de-watermark attestation: reviewer ≠ submitter check; `DEWATERMARK_DISCLAIMER`), `src/lcp/pipeline.py`/`processor` (record submitting actor at process/create time), `src/lcp/adapters/storage/audit_log.py` (attestation event), `src/lcp/core/config.py` (reviewer whitelist reuse)
- Test: `tests/test_dewatermark_attestation.py`

**Approach:**
- Record submitting actor at process/create time (no such identity today). Unlock requires: verifiable license-evidence field (contract id / URL / ownership proof) + approval by a whitelisted reviewer **≠ submitter** + audit event. `DEWATERMARK_DISCLAIMER` states "attestation not authentication; records responsibility, does not prevent infringement" (mirror `signoff.DISCLAIMER`). Default-locked; missing any → no de-watermark.

**Execution note:** Test-first on the segregation rule (approver == submitter must be rejected).

**Patterns to follow:** `signoff.backfill`/`DISCLAIMER`/reviewer-whitelist; `audit_log` append-only PII-free events.

**Test scenarios:**
- Happy path: evidence + reviewer≠submitter → unlocked; audit records evidence ref + reviewer + submitter.
- Error path: reviewer == submitter → rejected (no unlock).
- Error path: missing evidence → locked; empty/garbage evidence → rejected.
- Edge case: reviewer not in whitelist → rejected.
- Integration: attestation event is append-only + PII-free (evidence ref hashed/structured, not raw PII in the index).

**Verification:** De-watermark cannot run without an independent, evidenced, audited attestation; disclosure text is verbatim and honest.

- [ ] **Unit 8: De-watermark engine integration (isolated)**

**Goal:** Run the spike-chosen engine in an isolated subprocess, gated by attestation, with provenance, EXIF strip, idempotency, dry-run, and fail-closed behavior.

**Requirements:** UR2, UR4, UR14, UR15

**Dependencies:** Units 6 (GO) + 7

**Files:**
- Create: `src/lcp/adapters/media/dewatermark_runner.py` (subprocess isolation, mirrors `crawl_runner`), `src/lcp/adapters/media/mask.py` (config fixed-box / operator-box → mask)
- Modify: `src/lcp/adapters/processor/media_checker.py` (gate de-watermark before normalize, only if attested), `src/lcp/core/models.py` (`AssetRef` provenance: `watermark_removed` + evidence ref, PII-free), `pyproject.toml` (`[inpaint]` extra: onnxruntime + bundled MI-GAN-ONNX, **no torch**, OR external static-ghost call)
- Test: `tests/media/test_dewatermark_integration.py`

**Approach:**
- Isolated subprocess (scrubbed env, assets never leave machine) like `crawl_runner`; main `[media]` extra stays torch/opencv-free. Mask from config-box/operator-box (no auto-detect v1). Strip EXIF on output (don't pass `exif=`; `convert("RGB")`). Mark `watermark_removed=true` + evidence ref in manifest. Failure / low-confidence / large-tiled (out-of-scope) → `needs_revision`, **never silent partial**. Missing engine → `DependencyError` (mirror missing-ffmpeg). Idempotent + dry-run (dry-run writes no cleaned output). Fits inside PROCESSING→PROCESSED (no new JobState).

**Patterns to follow:** `crawl_runner` subprocess + `minimal_env`; `media_checker` `DependencyError`; `manifest` PII-free.

**Test scenarios:**
- Happy path: attested asset + valid mask → cleaned asset, `watermark_removed=true` + evidence ref, EXIF stripped (assert no GPS in output).
- Edge case: no watermark present to remove → no-op with clear status (not a failure).
- Edge case: out-of-scope (large/tiled) watermark → `needs_revision`, no partial output.
- Error path: engine missing/uninstalled → `DependencyError`.
- Error path: low-confidence/failed inpaint → `needs_revision`, no silent residual.
- Edge case: unattested asset → de-watermark never runs (gate from Unit 7).
- Integration: dry-run writes no cleaned file; re-run is idempotent.

**Verification:** Only attested assets are de-watermarked, in isolation, with provenance + EXIF-stripped output; failures fail closed; dry-run inert.

- [ ] **Unit 9: De-watermark GUI (attestation flow + inpaint interaction states)**

**Goal:** Operator surface for the attestation flow and the slow inpaint op, with correct interaction states and a raised poll cap.

**Requirements:** UR16

**Dependencies:** Units 7, 8

**Files:**
- Modify: `src/lcp/web/lex.js` (`LEX.honesty.dewatermark_attest`, `STATE_ACTIONS` gated action), `src/lcp/web/app.js` (locked/unlocked/already-attested states; evidence + reviewer form; **raise `POLL_CAP` for inpaint jobs**; per-asset progress where available), `src/lcp/gui.py` (`Api` de-watermark attestation + status; expose `DEWATERMARK_DISCLAIMER`), `src/lcp/cli.py` (CLI parity)
- Test: `tests/test_gui_dewatermark.py`

**Approach:**
- States: locked default (action absent/disabled + reason copy), attestation form (evidence input + independent reviewer select + honesty callout), unlocked, already-attested on re-entry. Slow-op: loading copy reusing the poller but with a **raised/removed `POLL_CAP`** so multi-image inpaint doesn't show a false ~90s timeout; partial-success / low-confidence-vs-failure surfaced. Strict CSP, `textContent`, honesty text from `LEX.honesty`.

**Patterns to follow:** `backfill`/`--attest` checkbox + `reviewerSelect` + `disclaimer()`; `STATE_ACTIONS` fail-closed gating; existing poller.

**Test scenarios:**
- Happy path: attestation form (evidence + independent reviewer) unlocks the de-watermark action; honesty callout shown.
- Edge case: reviewer==submitter selected in GUI → blocked with message.
- Edge case: already-attested job on re-entry → shows attested state, not the form again.
- Edge case: long multi-image inpaint exceeds old 90s cap → still polling (no false timeout).
- Integration (parity): CLI attestation+de-watermark reaches the same state as GUI.

**Verification:** Operator can attest (with independent reviewer) and run de-watermark without false timeouts; locked-by-default holds; CLI/GUI parity.

### Phase / Batch 3 — Ingest (smallest, by-need)

- [ ] **Unit 10: Mixed-folder material-pack ingest + completeness check**

**Goal:** Improve local material-pack import (mixed image/video/text in one folder) with a completeness check; reaffirm crawl scope honesty + the SSRF residual.

**Requirements:** UR11, UR12

**Dependencies:** None

**Files:**
- Modify: `src/lcp/adapters/crawler/ingest.py` (mixed-folder import + archive), `src/lcp/adapters/processor/media_checker.py` (completeness check reuse), `docs/security/pii-inventory.md` (re-state SSRF residual if allowlist widens)
- Test: `tests/test_ingest_mixed_folder.py`

**Approach:**
- Reuse `ingest.py` + `media_checker`; flag missing/unopenable items; keep Scrapy public-source + local import as the only channels (no JS/login-wall, no anti-bot bypass). Note `pii-inventory.md` SSRF residual remains if `allow_domains` grows.

**Test scenarios:**
- Happy path: folder with images+video+notes → all imported, classified, manifest built.
- Edge case: unopenable image / unplayable video → flagged in completeness report, not silently dropped.
- Edge case: empty folder / unsupported file types → clear status.
- Integration: imported pack flows into the existing media gate unchanged.

**Verification:** Mixed packs import with a completeness report; crawl scope/SSRF honesty preserved.

## System-Wide Impact

- **Interaction graph:** new steps live inside Stage-2 `run_media_gate` (watermark add/remove, cover checks) and the assemble step (templates, captions); no new JobState — de-watermark + attestation fit inside PROCESSING→PROCESSED.
- **Error propagation:** de-watermark/inpaint failures, low-confidence, out-of-scope, and missing-engine all converge to `needs_revision` / `DependencyError` (fail-closed); no silent partial media.
- **State lifecycle risks:** **freeze hash binding extended** to captions/image-sections (Unit 4) — without it, post-freeze AI-content edits go undetected. Submitter identity must be recorded at process/create time for SoD (Unit 7).
- **API surface parity:** every new operator action is CLI + GUI 1:1 (Units 5, 9); GUI `POLL_CAP` raised for inpaint jobs.
- **Integration coverage:** CLI/GUI parity via shared `_processed_job_with_draft`; freeze-binding + grounding are integration-tested, not just unit-mocked.
- **Unchanged invariants:** no auto-publish (R26/UR15); append-only PII-free audit (R38/R39); zero-capability LLM + datamarking (R16/R35) — templates render outside SYSTEM; SSRF posture and its documented residual unchanged.

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| De-watermark CPU latency on operator laptop unviable (no public data) | Med | High | Unit-6 spike measures wall-clock on the target laptop; if too slow, degrade to batch/background or cut Batch 2 |
| Inpainting deps conflict with Pillow 12 (`pillow<10` pins) | High (known) | High | Isolate engine in a subprocess/separate env or external static-ghost; main `[media]` extra stays torch/opencv-free; ONNX preferred |
| Self-attestation is "paper cover" for stripping third-party marks | Med | High | Net-new SoD (reviewer≠submitter) + verifiable evidence + honest disclosure (Unit 7); default-locked; R2 honestly framed as an amendment |
| Republication-of-source defamation (risk is in sourcing, not rewriting) | Med | High | Keep R16; add R4/R5 measurable 漏检率 acceptance bar (origin success criterion); AI limited to structural pieces |
| AI captions raise per-article review burden (goal regression) | Med | Med | Success criterion = net review-touch DROP; tighten generation scope if it rises; grounding + freeze-binding so review is trustworthy |
| User-editable templates as a new injection surface | Med | Med | Templates never reach SYSTEM; `str.format_map` allowlist (no Jinja2); linter on save+import; deterministic canary/schema |
| Cover text/3rd-party-watermark "hard rule" not automatable | High | Low | Demote to advisory + human preview; only geometry/border auto-warn |
| static-ghost is an external repo (licensing/maintenance) | Med | Med | Confirm license + maintenance as a Unit-6 go/no-go precondition if that engine is chosen |

## Documentation / Operational Notes

- README: document the `[inpaint]` extra (or static-ghost dependency), the offline weight handling (bundled MI-GAN-ONNX vs side-loaded LaMa-ONNX, never silent download), and that de-watermark is owned/licensed-only + attested.
- Honesty surfaces: `DEWATERMARK_DISCLAIMER` + cover-advisory wording must state limits (attestation≠authentication; aesthetic checks are hints).
- Spike artifact (Unit 6) is the go/no-go record for Batch 2.

## Alternative Approaches Considered

- **Inline inpainting in main venv (torch/simple-lama/IOPaint):** rejected — hard Pillow-12 conflict + heavy footprint for a non-technical laptop.
- **cv2.inpaint only (no deep model):** rejected as the primary path — weak on the block/semi-transparent platform watermarks that are the real case; kept only as a cheap spike baseline.
- **Auto watermark detection (Florence-2/SAM):** rejected v1 — torch-heavy, CPU-slow; use config-box/operator-box masks instead.
- **Jinja2 SandboxedEnvironment for templates:** rejected — anti-RCE not anti-prompt-injection, heavier, has escape CVEs; `str.format_map` allowlist is lighter and safer for this threat.
- **LLM-as-judge for template/caption safety:** rejected — extra latency on an offline laptop; no tool-calling means blast radius is "low-quality text"; deterministic canary + schema suffices.

## Phased Delivery

- **Batch 1 (Units 1–5):** watermark-add + cover checks + templates + AI captions (with grounding/freeze) + GUI. Lands the daily-value, low-risk wins.
- **Batch 2 (Units 6–9):** **Unit 6 spike first** → go/no-go; only on GO build attestation (7) + integration (8) + GUI (9). Cuttable without affecting Batch 1.
- **Batch 3 (Unit 10):** ingest improvement, by-need.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-17-content-pipeline-upgrade-requirements.md](../brainstorms/2026-06-17-content-pipeline-upgrade-requirements.md)
- **SOP (source of truth):** `docs/spec/远程内容编辑发布流程SOP_新媒体版.md`
- **Baseline plan:** `docs/plans/2026-06-16-001-feat-local-content-processor-mvp-plan.md`; **PII/SSRF residual:** `docs/security/pii-inventory.md`
- Existing engine candidate: `redredchen01/static-ghost` (LaMa+OpenCV+FFmpeg) / local `static-ghost-run` skill
- External: Pillow 12.x docs (alpha_composite/exif_transpose/Exif); `simple-lama-inpainting` 0.1.2 / `IOPaint` 1.6.0 (Pillow pins); MI-GAN-ONNX (MIT) / LaMa-ONNX (Apache-2.0); OWASP/Microsoft prompt-injection guidance; spectral-residual saliency (Hou & Zhang 2007)
