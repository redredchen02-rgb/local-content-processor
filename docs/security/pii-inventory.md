# PII Inventory & Data-Flow (R43, gating)

Status: gating deliverable for Unit 3. Lists every place PII (or
attacker-shapeable text) can come to rest, and how each is governed in the MVP.

## MVP governance model (R42, confirmed scope)

- **No at-rest encryption / crypto-shredding in MVP.** PM/legal have confirmed
  there is **no named statutory erasure obligation** for this system. (This
  confirmation should be recorded in writing here so the assumption is
  auditable — see "Sign-off" below.) Disk encryption + crypto-shredding are
  **post-MVP**.
- **Deletion = best-effort `unlink`/`rmtree`** of the job directory plus an
  `ERASURE` audit event. We **do not claim cryptographic erasure**: SSD
  wear-leveling, snapshots, and swap may retain copies. Operators are advised
  to enable OS-level full-disk encryption.
- **Plaintext at rest, 0600.** `data/jobs/` blobs are plaintext protected only
  by `0600`/`0700` permissions (umask `0o077` set at startup).
- **PII-free index.** SQLite and the manifest hold no raw PII by construction.
- **Sensitive payload is separated from the audit log.** Audit carries only
  job IDs, stage/event codes, an actor name, and high-entropy artifact content
  hashes — never raw identifiers.

## PII sinks

| Sink | Path | PII? | Governance |
|------|------|------|------------|
| Raw bundle | `data/jobs/<id>/raw/` | **Yes** (scraped html/text/media, may embed names, phones, URLs) | Plaintext 0600; best-effort deletion |
| Processed bundle | `data/jobs/<id>/processed/` | **Yes** (LLM draft + reports) | Plaintext 0600; best-effort deletion |
| Review packet | `data/jobs/<id>/review/` | **Yes** (sanitized draft, cover, title, lazy source link text) | Plaintext 0600; best-effort deletion; output sanitized (R41) |
| SQLite `jobs` index | `data/lcp.db` (`jobs` table) | **No** (PII-free by construction) | Only allowed columns persisted (below) |
| SQLite `saved_sources` | `data/lcp.db` (`saved_sources` table) | **Yes** — DELIBERATE EXCEPTION (plaintext `source_ref` URL/path + free-text `label`) | Plaintext by design so a source can be re-submitted (a hash can't be re-crawled); physically separate table/module (`source_store.py`); best-effort deletion; erasure path below; CRUD never writes plaintext to audit |
| Manifest | `data/jobs/<id>/manifest.json` | **No** (kept PII-free; per-asset status + hashes only) | Atomic commit; no scraped title/body/PII-bearing URL |
| Audit log | `data/jobs/<id>/audit.jsonl` | **No** (PII-free; hashes + codes only) | Append-only + hash chain; `append()` rejects PII keys; deleted with job dir |
| Logs | stdout / log handlers | **No** (secrets masked) | `SecretRedactingFilter` (Unit 2); no raw payload logged |
| Temp files | inside `data/jobs/<id>/` only (not `/tmp`, R44) | **Maybe** transient | Same-dir temp + `os.replace`; removed on commit; covered by job-dir deletion |
| Keyring | OS keyring (`local-content-processor`) | **Secret** (api_key) | Never written to file/db/log; not a PII-of-subjects sink |

## SQLite allowed vs prohibited columns

**Allowed (PII-free):** `job_id`, `state`, `created_at`, `updated_at`,
`source_html_sha256`, `source_text_sha256`, `error_code`,
`review_reason` (**enum code only**: `risk` / `dedup` / `grounding`).

**Prohibited (never persist to SQLite):** title, article body/text, source
URL, author, domain, and **`review_reason` as free text** (must be the enum
code, not a human sentence).

The same prohibition applies to `manifest.json` and `audit.jsonl`.

**Exception — `saved_sources` table.** This prohibition governs the `jobs`
table. The separate `saved_sources` table (input-reuse feature) deliberately
stores plaintext `source_ref` and free-text `label` (see the PII sinks row).
It is the only plaintext-identifier store in `lcp.db`; it is isolated in its own
module (`source_store.py`) and its own erasure path so the `jobs` PII-free
invariant is unaffected.

## Audit log hashing rule

- Hash chain: `line_hash = sha256(prev_hash + canonical(line))`. **Tamper-
  evident, not tamper-proof** — a local root user can recompute the chain; we
  cannot prevent that. The chain only makes silent edits detectable.
- Only **high-entropy artifact CONTENT hashes** (sha256 of a draft/cover/etc.)
  may be recorded. **Never hash a bare low-entropy identifier** (phone, name) —
  such hashes are brute-forceable and count as PII.

## Erasure flow

Delete job → `rmtree(data/jobs/<id>/)` (best-effort) → delete SQLite row →
append `ERASURE` audit event (`method=best_effort_unlink`,
`cryptographic_erasure=false`). The audit chain after deletion still verifies.

**`saved_sources` erasure (plaintext PII-exception table).** `saved_sources`
rows are NOT job-linked, so job deletion does not reach them; they have their
own entry points in `source_store.py`:
- **Per-URL / per-subject:** `SourceStore.delete_by_source_ref(source_ref)`
  removes every saved row carrying that plaintext source.
- **Single entry:** `SourceStore.delete_source(id)` (the GUI's
  `delete_saved_source` action).
- **Full wipe / reset:** `SourceStore.delete_all()`.
All are **best-effort** (SQLite `DELETE` does not zero freed WAL/freelist
pages — same honesty boundary as job blobs; protection relies on OS full-disk
encryption + 0600). Erasure auditing here records the opaque id only, never the
plaintext `source_ref`/`label`.

## SSRF residual risk (honest gap, R40)

The crawl path's ACTIVE SSRF defences are: scheme allowlist (http/https) +
DNS-resolved `is_global` check at validate time (top-level URL **and** every
scraped media URL) + Scrapy `allowed_domains` (OffsiteMiddleware) +
`REDIRECT_ENABLED=False` (redirects not followed).

**Residual (documented, not fixed):** pin-IP-at-connect is **NOT wired** for the
Scrapy path. `net_guard.ValidatedTarget.pinned_ip` and
`net_guard.revalidate_redirect()` exist but nothing in the live crawl consumes
them, because Scrapy opens its own connection and **re-resolves DNS at connect
time**. We cannot force Scrapy to connect to the literal IP we validated, so
**DNS-rebinding / TOCTOU on an allowlisted domain is a residual risk**: an
attacker controlling an allowlisted domain's DNS could answer with a global IP at
validate time and an internal IP at connect time.

- Why not fixed now: wiring a pinned-IP custom resolver into the Scrapy
  subprocess is heavy; deferred to a future pinned-connection transport (e.g. an
  httpx fetcher that connects to the literal IP with a Host-header override).
- `pinned_ip` / `revalidate_redirect` are kept (and tested) as drop-in building
  blocks for that future transport — they are **not** claimed as active today.
- Mitigations in place meanwhile: tight `allowed_domains`, scheme allowlist, the
  per-resolved-IP `is_global` reject (rejects any internal A/AAAA record at
  validate time), and no redirect following.

## Sign-off (to be completed by PM/legal)

- [ ] Confirmed in writing: no named statutory erasure obligation for MVP.
      Owner: ______  Date: ______
- [ ] Operators informed that `data/jobs/` is plaintext 0600 and deletion is
      best-effort (SSD erasure not guaranteed); OS full-disk encryption advised.
