# lcp benchmark spike

Performance benchmark that measures key operational metrics for `lcp`. Designed
for CI consumption (JSON output) and manual baseline profiling.

## Metrics measured

| Key | What it measures |
|-----|-----------------|
| `startup_list_ms` | `list_jobs` on an empty workspace — interpreter + import latency |
| `list_jobs_ms` | `list_jobs` on N synthetic jobs — SQLite read throughput |
| `batch_summary_ms` | `batch_summary` — summary aggregation cost |
| `process_single_ms` | Full `Pipeline.process` on one job (mocked LLM, real gates) |
| `peak_memory_mb` | `tracemalloc` peak during `process_single` |

## Usage

```sh
# Human-readable table (default)
./.venv/bin/python spikes/benchmark/run.py

# JSON to stdout (CI / comparison scripts)
./.venv/bin/python spikes/benchmark/run.py --json

# Increase synthetic job count for list-latency stress
./.venv/bin/python spikes/benchmark/run.py --jobs 100
```

## CI integration

The release workflow (`.github/workflows/release.yml`) runs this benchmark on
release branches and prints a regression warning if any metric regresses >20%
vs the last tagged release. No hard failure — regressions surface as a warning
to inform the release decision.

## Baseline (2026-06-22, MacBook M-series, Python 3.11-slim)

Approximate values — exact numbers vary by machine:

```
  startup list:      ~12 ms
  list 20 jobs:      ~25 ms
  batch summary:     ~5 ms
  process single:    ~180 ms  (NEEDS_REVISION — short title)
  peak memory:       ~18 MB
```

## Design notes

- Uses a real `Pipeline` + `JobStore` + `AuditLog` in a `tempfile.mkdtemp()` workspace.
- LLM calls are replaced by `_BenchmarkLlmClient` (no network, no API key required).
- Mirrors the `spikes/detection_accuracy/run_eval.py` structure — each spike is a
  self-contained script with a `--json` flag for machine consumption.
