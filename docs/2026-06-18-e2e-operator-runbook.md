# Operator runbook — one job, end to end (2026-06-18)

A copy-pasteable go/no-go for taking **one** job from raw material to a frozen,
signed-off, recorded review packet. This is the exact path the automated e2e test
(`tests/test_e2e_pipeline.py`) walks, so the commands here stay honest.

> The machine **never publishes**. `backfill` only *records* that a human pasted
> the published URL and attested it matches the signed-off version.

## 0. One-time setup

```sh
python3.11 -m venv .venv && ./.venv/bin/pip install -e ".[crawl,media,llm,dedup,gui,dev]"
# ffmpeg/ffprobe must be on PATH (media gate).
./.venv/bin/lcp init          # config.yaml (0600) + empty data/site_index.jsonl
```

Then edit `config.yaml`:

- `publisher.reviewers` — **add at least one reviewer name** (else `approve`/
  `resolve`/`backfill` refuse — they require a whitelisted reviewer).
- `crawler.allow_domains` — only sources you own/may legally cite (URL crawls).
- `llm.base_url` (OpenAI-compatible `/v1`) + `llm.allowed_hosts`; put the API key
  in the keyring (`keyring set local-content-processor llm`) or `LCP_LLM_API_KEY`.

## 1. Happy path (the go path)

```sh
# Stage 1 — bring in material (no network):
lcp ingest --job-id job-001 --dir ./material/job-001
#   (a folder with title.txt + one of source/body/content/text.txt + optional images)

# Stage 2 — process. --ai-copy is REQUIRED for a complete draft (it fills
# quick_facts / summary / FAQ, and captions for image bundles). Title 25-35 chars.
lcp process --job-id job-001 --ai-copy \
    --title "A working title of twenty-five to thirty-five chars"
#   -> expect: processed job-001: processed
#   one-shot equivalent: lcp run --job-id job-001 --input ./material/job-001 \
#                           --until review --title "<25-35 chars>"   (ai-copy on by default)

# Stage 4 — freeze the sanitized packet:
lcp review-packet --job-id job-001 --source-url https://source.example/post/123
#   -> review_pending; a body_sha256 is recorded

# Sign-off (attribution, not auth) + record the human publish:
lcp approve  --job-id job-001 --reviewer alice
lcp backfill --job-id job-001 --reviewer alice \
    --url https://your-site.example/published/job-001 --attest
#   -> published_recorded
```

## 2. When a job parks (the no-go branches)

`process` stops at the **first** gate that parks the job; the CLI prints the
resting state, the reasons (notes), and an advisory.

| Resting state | Meaning | Operator move |
|---|---|---|
| `BLOCKED` | redline content (terminal) | do not recover in place; only `supersede` (with second confirmation for a redline) starts a fresh job |
| `DUPLICATE` | matches the site index | `supersede` if it is genuinely new; else drop |
| `NEEDS_HUMAN_REVIEW` (dedup/risk) | uncertain | `lcp resolve --job-id X --reviewer alice --reason "..."` (recorded override) |
| `NEEDS_HUMAN_REVIEW` (grounding) | an ungrounded claim | fix the draft/source, then `lcp resolve --job-id X --reviewer alice --relint` |
| `NEEDS_REVISION` | lint (missing sections, title length, copied-too-much) | see the notes for which sections; re-run `process` with `--ai-copy` |

**Dry-run caveat:** `--dry-run` never calls the LLM, so the copywriter sections
stay empty and the job **cannot reach a packet** — use it only to smoke the
deterministic gates, not to produce a real packet.

## 3. Optional — validate against a real LLM endpoint

The deterministic suite never touches the network. To validate the real
assemble + copywriter + grounding round-trip (closes the deferred PR #5 check):

```sh
export LCP_LLM_API_KEY="sk-..."
export LCP_LIVE_LLM_BASE_URL="https://your-endpoint.example/v1"
export LCP_LIVE_LLM_MODEL="your-model"
./.venv/bin/python -m pytest tests/test_live_llm_lane.py -q
```

Without those env vars the lane is **skipped** (so CI never runs it). It uses a
synthetic fixture — never real scraped subject PII.

## 4. Go/no-go checklist

- [ ] `lcp init` run; `config.yaml` has ≥1 reviewer; LLM key in keyring/env.
- [ ] `lcp process --ai-copy` reaches `processed` (not `needs_revision`).
- [ ] `review-packet` shows a `body_sha256`; `data/jobs/<id>/` artifacts parse.
- [ ] `approve` then `backfill --attest` reaches `published_recorded`.
- [ ] Any parked job was resolved by a **named** reviewer with a recorded reason.
