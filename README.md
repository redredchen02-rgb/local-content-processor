# local-content-processor (`lcp`)

A **local content pipeline**: it takes a source (a URL on your own allowlisted,
legally-citable site, or a local folder of material), runs it through

```
crawl / ingest  ->  process  ->  review packet
```

and stops there. The MVP **deliberately ends before publishing**. A human reviews
the frozen packet, signs off, publishes by hand to their own CMS, and then records
the published URL back into `lcp`. The machine never writes to a CMS.

It is built for a **non-technical operator** to drive (every action has a CLI
command, and a minimal local GUI mirrors them 1:1), and it is **own-site,
compliance-first**: tight domain allowlists, robots.txt obeyed, SSRF guards, and
no auto-publish.

> Honest scope: this is an MVP. Several things are intentionally *not* built yet
> (see [Deferred / not in MVP](#deferred--not-in-mvp)). The README does not claim
> capabilities the code does not have.

## What it does

- **Crawl / ingest (Stage 1)** — fetch a URL via a sandboxed Scrapy subprocess
  (one subprocess per job; secrets stripped from its environment), or ingest a
  local material folder with no network. Output: a raw bundle (source HTML/text,
  per-asset media, a PII-free manifest with sha256 hashes).
- **Process (Stage 2)** — validate/normalize media (ffmpeg/ffprobe), run the
  risk gate (defamation/privacy/redlines), the dedup gate, then a
  constrained-rewrite assemble step (the LLM), then lint + grounding checks.
  Any gate that fails *fail-closed*: the job parks at a hold state for a human
  rather than auto-passing.
- **Review packet (Stage 4)** — freeze the exact processed draft into a sanitized
  packet (title, body, cover, inert source-link text) and move the job to
  `REVIEW_PENDING`. From here on the draft is immutable (the state machine has no
  edge back into processing).
- **Sign-off** — a whitelisted reviewer approves/rejects. Approval is *not*
  publication; the loop only closes when a human pastes the published URL back in
  and attests it matches the signed-off version.

## Install / setup

Requires **Python 3.11+** and **ffmpeg / ffprobe** on `PATH` (media
normalization shells out to them).

```sh
python3.11 -m venv .venv
./.venv/bin/pip install -e ".[crawl,media,llm,dedup,gui,dev]"
```

Optional-dependency groups (install only what you need):

| extra   | brings in            | used by                         |
|---------|----------------------|---------------------------------|
| `crawl` | scrapy               | URL crawling (Stage 1)          |
| `media` | pillow               | image processing                |
| `llm`   | openai               | the constrained-rewrite step    |
| `dedup` | datasketch           | dedup index                     |
| `gui`   | pywebview            | the minimal local GUI           |
| `dev`   | pytest               | the test suite                  |

Configuration:

```sh
lcp init     # writes config.yaml (0600) + seeds an empty data/site_index.jsonl
# then edit config.yaml: allow_domains, reviewers, llm.base_url, etc.
```

`lcp init` is idempotent and never clobbers an existing `config.yaml`. The empty
`site_index.jsonl` it seeds is what lets a fresh clean job pass the dedup gate
(an absent index makes the gate fail-loud and park every job for human review).

Subsequent commands **auto-load `./config.yaml` from the current directory** — so
`lcp init` → edit → `lcp run` just works, no `--config` needed; `--config PATH`
overrides, and with no `config.yaml` present the built-in defaults apply. Because
the file is read by location, run `lcp` from your own working directory (or pass
`--config` explicitly) when in doubt — a `config.yaml` sitting in the directory you
happen to be in is loaded as-is, including its `allow_domains` and `reviewers`.

**The LLM API key is never stored in `config.yaml`.** Put it in the OS keyring
(service `local-content-processor`, user `llm`), or set the `LCP_LLM_API_KEY`
environment variable:

```sh
# keyring (preferred)
keyring set local-content-processor llm
# or env var
export LCP_LLM_API_KEY="sk-..."
```

Key things to set in `config.yaml` before a real run:

- `crawler.allow_domains` — only public, legally-citable sources you own/may cite
  (each needs a legal basis). An empty allowlist rejects everything.
- `publisher.reviewers` — the whitelist of reviewer names allowed to approve/reject.
- `llm.base_url` (an OpenAI-compatible `/v1` endpoint) and `llm.allowed_hosts`.

## Quickstart (CLI)

A single job, from crawl to a frozen review packet, then sign-off and backfill.
(`lcp` is installed as a console script; you can also run `./.venv/bin/lcp`.)

```sh
# 1. Crawl one URL into a raw bundle (Stage 1). The domain must be allowlisted.
lcp crawl --job-id acme-001 --url https://your-allowlisted-site.example/post/123
#   (local material instead of a URL:)
# lcp ingest --job-id acme-001 --dir ./material/acme-001

# 2. Process: media + risk + dedup + assemble + copywriter + lint/grounding.
#    A COMPLETE draft needs --ai-copy (it fills quick_facts/summary/FAQ, and
#    captions for image bundles); the title must be 25-35 chars. Note: --dry-run
#    runs the deterministic stages but does NOT call the LLM, so it cannot reach
#    PROCESSED (no copywriter sections) — use it only to smoke the gates.
lcp process --job-id acme-001 --title "A working title of twenty-five to thirty-five chars" --ai-copy

# 3. Freeze the reviewed artifact into a sanitized packet (Stage 4).
lcp review-packet --job-id acme-001 --source-url https://your-allowlisted-site.example/post/123

# 4. A whitelisted reviewer signs off (attribution, not authentication).
lcp approve --job-id acme-001 --reviewer alice

# 5. The human publishes by hand, then records the URL and attests it matches.
lcp backfill --job-id acme-001 --reviewer alice \
    --url https://your-site.example/published/acme-001 --attest
```

Holds and worklist:

```sh
# A parked job (risk/dedup/grounding hold) is driven out by a human:
lcp resolve --job-id acme-002 --reviewer alice --reason "verified, false positive"
lcp resolve --job-id acme-003 --reviewer alice --relint    # clear a grounding hold

# Reject a pending job (terminal):
lcp reject --job-id acme-004 --reviewer alice --reason "off-policy source"

# Worklist + batch view:
lcp list                  # all jobs and their states
lcp list --state blocked  # filter by state
lcp list --summary        # counts-by-state

# End-to-end in one call, up to a target (--ai-copy is ON by default for `run`;
# pass --no-ai-copy to skip the copywriter). Title must be 25-35 chars.
lcp run --job-id acme-005 --url https://your-allowlisted-site.example/post/9 \
    --until review --title "A working title of twenty-five to thirty-five chars"
```

Global flags: `--config PATH`, `--dry-run`, `--json` (machine-readable),
`--quiet`, `--output-dir DIR`. Exit codes follow the error contract
(`0` ok, non-zero per error type).

The GUI is the same operator surface (`Api` in `src/lcp/gui.py`); it must launch
on a real desktop (`lcp` exposes the headless logic; the window itself needs
`pywebview` and a display).

## Pipeline stages & job state machine

Stages: **1** crawl/ingest -> **2** process (media, risk, dedup, assemble, lint,
grounding) -> **4** review packet (freeze) -> human sign-off. (Stages 5/6,
auto-publish, are out of scope.)

The job lifecycle is a pure transition table (`src/lcp/core/state.py`). The
happy path:

```
NEW -> CRAWLED -> PROCESSING -> PROCESSED -> REVIEW_PENDING -> APPROVED -> PUBLISHED_RECORDED
```

Side branches (all fail-closed):

- `CRAWLED_WARN` (partial assets), `CRAWL_FAILED` / `PROCESS_FAILED` (retriable),
  `NEEDS_REVISION` (incomplete content).
- `BLOCKED` (hard risk redline) and `DUPLICATE` are **terminal**.
- `NEEDS_HUMAN_REVIEW` carries a reason code (`risk` | `dedup` | `grounding`) and
  exits via `resolve` -> `PROCESSED`, or `reject`/`supersede`.
- `REVIEW_PENDING` has **no edge back to `PROCESSING`** — that absence *is* the
  freeze guarantee. It exits via `approve`, `reject`, or `supersede`.

Terminal states: `BLOCKED`, `DUPLICATE`, `REJECTED`, `SUPERSEDED`,
`PUBLISHED_RECORDED`. `PROCESSING` is transient and never persisted as a resting
state.

## Security & compliance posture (stated honestly)

- **No publish without a human.** The machine never writes to a CMS. `approve` is
  not publication; `backfill --attest` only *records* a human's paste + attestation.
- **Reviewer sign-off is attribution, not authentication.** It records a stated
  reviewer name (checked against the whitelist) and the observed OS user, with a
  verbatim disclaimer. It does not prove identity.
- **PII at rest is plaintext, `0600`.** Job bundles live under `data/jobs/<id>/`
  protected only by file permissions (umask `0o077` at startup). The SQLite index,
  manifest, and audit log are **PII-free by construction** (hashes + codes only).
- **Deletion is BEST-EFFORT, not cryptographic erasure.** Delete = `rmtree` the
  job dir + drop the SQLite row + an `ERASURE` audit event. We do **not** claim
  crypto-shred: SSD wear-leveling, snapshots, and swap may retain copies. Enable
  OS full-disk encryption.
- **LLM zero-capability + output escaping.** The LLM client only makes a single
  Chat Completions call returning text — no tools, no link-following, no writes.
  Attacker-shapeable fields are HTML-escaped, and source URLs are rendered as
  inert text (never a live `<a href>` / never fetched), on both the packet and the
  GUI bridge.
- **SSRF guards.** Scheme allowlist (http/https), DNS-resolved `is_global` check
  on the top-level URL *and* every scraped media URL, Scrapy `allowed_domains`,
  and `REDIRECT_ENABLED=False`.
- **Documented residual:** pin-IP-at-connect is **not** wired for the Scrapy path
  (Scrapy re-resolves DNS at connect time), so **DNS-rebinding / TOCTOU on an
  allowlisted domain is a known, accepted residual risk**. See
  [`docs/security/pii-inventory.md`](docs/security/pii-inventory.md).

## Deferred / not in MVP

- **The U1 accuracy decision** (substring grounding vs `+NLI`/MiniCheck) — needs a
  real labeled corpus; the spike measures mechanics only, not a go/no-go.
- **A live company LLM endpoint** — the client is wired and tested; pointing it at
  a real internal endpoint is config + ops.
- **Encryption-at-rest / crypto-shred** — post-MVP (no named statutory erasure
  obligation confirmed for MVP).
- **Stage 5/6 auto-publish** — out of scope by design.
- **Playwright** (JS-rendered crawling) — not used; Scrapy only.
- **Perceptual-hash dedup** — not implemented.
- **GUI window** — the operator logic is headless-testable, but the actual window
  must launch on a real desktop.

## Running tests

```sh
./.venv/bin/python -m pytest -q
```

The detection-accuracy spike (mechanics harness, prints a precision/recall
decision table; does **not** make the accuracy decision):

```sh
./.venv/bin/python spikes/detection_accuracy/run_eval.py
./.venv/bin/python spikes/detection_accuracy/run_eval.py --json
```

## Layout

```
src/lcp/
  core/        pure functional core (models, rules, draft, state) — NO I/O
  adapters/    imperative shell: crawler, media, llm, processor, publisher, storage
  pipeline.py  injects adapters, runs stages, drives the state machine
  cli.py       thin CLI shell (every operator action)
  gui.py       minimal pywebview js_api shell (mirrors the CLI)
docs/          plans, brainstorms, security (pii-inventory.md)
spikes/        detection_accuracy/ measurement harness
tests/         pytest suite
```
