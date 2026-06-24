---
title: "CRAWL_FAILED retry loses source URL — write-before-set_state + retry read-back"
date: "2026-06-24"
category: ui-bugs
module: pipeline / gui / web
problem_type: ui_bug
component: tooling
severity: medium
symptoms:
  - "CRAWL_FAILED job retry opens create-job dialog with blank URL field"
  - "Operator must retype source URL from memory after a failed crawl"
  - "source.json absent on disk when crawl errors before write-before-set_state was wired"
root_cause: missing_workflow_step
resolution_type: code_fix
tags:
  - crawl-failed
  - retry
  - source-url
  - persistence
  - ux
  - stage1
  - gossip-guard
  - write-before-set_state
---

# CRAWL_FAILED retry loses source URL — write-before-set_state + retry read-back

## Problem

When a URL-type job enters `CRAWL_FAILED` state and the operator retries it, the create-job dialog opens blank — the source URL is gone and must be retyped from memory. The root cause was that `pipeline.stage1()` never wrote `source.json` for URL-type jobs (only the gossip pipeline wired it), and the read-back API and JS auto-fill hook did not exist.

## Symptoms

- Operator opens a failed job and clicks "Retry"
- The create-job dialog appears with the URL field empty
- No auto-fill occurs even though the operator submitted that URL moments ago
- Affects all URL-type jobs (`SourceType.URL`); gossip jobs were unaffected because the gossip pipeline already wrote `source.json`

## What Didn't Work

No dead-ends — root cause was identified directly from code inspection: `source.json` was only written by the gossip pipeline path, not by `pipeline.stage1()` for URL-type jobs. The read-back API (`get_source_url`) and JS auto-fill hook did not exist at all, so there was nothing to fix on the frontend until the persistence gap was closed first.

## Solution

Three coordinated changes across `pipeline.py`, `gui.py`, and `app.js`.

### 1. `pipeline.py` — write `source.json` at Stage-1 entry

```python
_URL_SCHEMES = {"http", "https"}

def _try_write_url_source(spec: SourceSpec) -> None:
    """Called at Stage-1 entry for NEW jobs. Best-effort; never raises."""
    if spec.source_type is not SourceType.URL or not spec.url:
        return
    if urlparse(spec.url).scheme.lower() not in _URL_SCHEMES:
        return
    try:
        _write_source(spec.job_dir, url=spec.url, platform="url", title="")
    except Exception:  # noqa: BLE001
        logger.warning("stage1: source.json write failed for new job %s (best-effort)", spec.job_id, exc_info=True)

def _try_overwrite_url_source(spec: SourceSpec) -> None:
    """Called at Stage-1 entry for CRAWL_FAILED retries. Fail-closed on unreadable files."""
    if spec.source_type is not SourceType.URL or not spec.url:
        return
    if urlparse(spec.url).scheme.lower() not in _URL_SCHEMES:
        return
    source_path = spec.job_dir / SOURCE_NAME
    if source_path.exists():
        try:
            data = json.loads(source_path.read_text(encoding="utf-8"))
            platform = data.get("platform", "") if isinstance(data, dict) else ""
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "stage1: source.json unreadable for retry %s (fail-closed)", spec.job_id, exc_info=True
            )
            return  # unreadable → unknown origin → leave untouched
        if platform != "url":
            return  # gossip or other platform → leave untouched
    try:
        _write_source(spec.job_dir, url=spec.url, platform="url", title="")
    except Exception:  # noqa: BLE001
        logger.warning("stage1: source.json write failed for retry %s (best-effort)", spec.job_id, exc_info=True)
```

Both helpers are called **before** `set_state(CRAWLED)` — if the write fails the state transition hasn't been committed (crash-safe ordering). The retry variant is fail-closed on unreadable JSON and refuses to overwrite a `source.json` whose `platform != "url"` — protecting gossip jobs.

### 2. `gui.py` — new `get_source_url` API method

```python
@bridge_safe
def get_source_url(self, job_id: str) -> dict:
    from .adapters.crawler.net_guard import safe_join
    c = self._ro_ctx_get()
    if c.store.get_job(job_id) is None:
        return {"found": False, "url": None}
    source_path = safe_join(c.store.jobs_root, job_id) / gi.SOURCE_NAME
    if not source_path.exists():
        return {"found": False, "url": None}
    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"found": False, "url": None}
    if not isinstance(data, dict) or data.get("platform") != "url":
        return {"found": False, "url": None}
    url = data.get("url")
    if not isinstance(url, str) or not url:
        return {"found": False, "url": None}
    from urllib.parse import urlparse
    if urlparse(url).scheme.lower() not in {"http", "https"}:
        return {"found": False, "url": None}
    return {"found": True, "url": url}
```

Security layers: `safe_join` prevents path traversal; DB existence check before filesystem access; `platform == "url"` guard blocks surfacing gossip URLs through this API; scheme allowlist at the read path mirrors the write-path guard.

### 3. `app.js` — auto-fill the URL field on retry

```javascript
if (jobId) {
  BRIDGE.get_source_url(jobId).then(function (res) {
    // Only auto-fill if the operator hasn't typed anything yet and hasn't switched mode.
    if (res && res.found && !$("create-url").value.trim() && !$("create-mode-dir").checked) {
      $("create-url").value = res.url;
      setCreateMode(true);
    }
  }).catch(function () {});
}
```

The guard prevents clobbering operator input that arrived before the async response. `.catch(function () {})` makes auto-fill fire-and-forget — the dialog is fully usable regardless of API outcome.

## Why This Works

The root cause was a **persistence gap**: `source.json` was only written by the gossip pipeline's code path. Because the file never existed for URL-type jobs, there was nothing to read back and the dialog was always blank.

The fix closes the gap at the earliest point where both `job_dir` and `url` are known (Stage-1 entry). The retry variant is stricter because it arrives at a directory that already has state — silently mutating a gossip job's `source.json` would be a correctness bug, so it checks `platform` before overwriting.

The three-layer structure (persist → API → UI) matches the project's functional-core / imperative-shell architecture: the `_URL_SCHEMES` constant is shared by both write helpers; `get_source_url` is a thin reader with defense-in-depth validation; the JS is a best-effort UX enhancement that degrades gracefully.

The write is also correctly placed **outside** the `BEGIN IMMEDIATE` lock (consistent with the marker-file constraint documented in `begin-immediate-isolation-level.md`).

## Prevention

- Any new `SourceType` with a canonical URL concept must wire up `source.json` write in `stage1()` alongside the existing pattern — do not leave it to the feature's own code path. The `source.json` contract (`platform`, `url`, `title`) is the project-wide mechanism; new source types should write it at the same Stage-1 callsite.
- **Test the retry path end-to-end**, not just the happy path. The original bug survived green tests because the CRAWL_FAILED → retry → dialog auto-fill path was never integration-tested.
- Unit-test `_try_write_url_source` / `_try_overwrite_url_source` in a tmp job dir. Mandatory cases: non-URL `SourceType` (no write), non-http/https scheme (no write), OSError swallowed (no raise), retry with `platform="gossip"` (no overwrite), retry with malformed JSON (no overwrite, no raise), retry with `platform="url"` (overwrites).
- Unit-test `get_source_url` with: missing job, missing file, unreadable file, `platform != "url"`, non-http/https scheme, crafted `job_id` containing `../` (path traversal guard), happy path.

## Related Issues

- `atomic-write-temp-replace.md` — sibling crash-safe write pattern (atomic swap vs. write-before-set_state ordering; different failure modes, same discipline)
- `localhost-http-api-csrf-defense.md` — `safe_join` + scheme-allowlist rationale for GUI API endpoints; `get_source_url` is a concrete instance of those invariants
- `begin-immediate-isolation-level.md` — the source.json write is intentionally placed outside the `BEGIN IMMEDIATE` block, consistent with the marker-file constraint documented there
