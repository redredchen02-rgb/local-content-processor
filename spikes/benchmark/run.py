#!/usr/bin/env python3
"""Performance benchmark for lcp — measures key operational metrics.

Outputs JSON to stdout for CI consumption. Tracks:
  - Startup latency (lcp list on empty workspace)
  - List latency (lcp list on N synthetic jobs)
  - Processor throughput (process a small job, mocked LLM)
  - Memory peak (tracemalloc during processing)

Run:
    ./.venv/bin/python spikes/benchmark/run.py
    ./.venv/bin/python spikes/benchmark/run.py --json
    ./.venv/bin/python spikes/benchmark/run.py --jobs 50
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

# Ensure the project src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from lcp.adapters.llm.client import ChatResult
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config
from lcp.pipeline import Pipeline, batch_summary, list_jobs


class _BenchmarkLlmClient:
    """Fake LLM client that returns a fixed response without API calls."""

    dry_run = False

    def __init__(self) -> None:
        self._dry_run = False

    def chat(
        self, *, messages: list[dict[str, str]], max_tokens: int = 2048, **kw: object
    ) -> ChatResult:
        return ChatResult(
            text="Benchmark article body. " * 50,
            finish_reason="stop",
            model="benchmark",
            executed=True,
        )


def _seed_jobs(store: JobStore, n: int) -> None:
    """Create N synthetic CRAWLED jobs for list/throughput benchmarks."""
    from lcp.core.state import JobState

    for i in range(n):
        jid = f"bench-{i:04d}"
        store.create_job(jid, created_at="2026-06-22T00:00:00Z")
        store.set_state(jid, JobState.CRAWLED, updated_at="2026-06-22T00:00:00Z")
        # Write a minimal source.txt so process doesn't fail on read.
        raw_dir = store.job_dir(jid) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "source.txt").write_text(
            f"Benchmark article {i}. This is test content for performance measurement. " * 20,
            encoding="utf-8",
        )


def run_benchmark(num_jobs: int = 20) -> dict[str, object]:
    """Run all benchmarks and return results as a dict."""
    results: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="lcp_bench_") as tmp:
        data_dir = Path(tmp) / "data"
        store = JobStore(base_dir=data_dir)
        audit = AuditLog(data_dir / "audit.jsonl")
        config = Config()

        # --- Startup latency ---
        t0 = time.perf_counter()
        _ = list_jobs(store)
        results["startup_list_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # --- Seed jobs for batch benchmarks ---
        _seed_jobs(store, num_jobs)

        # --- List latency (N jobs) ---
        t0 = time.perf_counter()
        jobs = list_jobs(store)
        results["list_jobs_count"] = len(jobs)
        results["list_jobs_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # --- Batch summary ---
        t0 = time.perf_counter()
        summary = batch_summary(store)
        results["batch_summary_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results["batch_summary"] = summary

        # --- Processor throughput (single job, mocked LLM) ---
        fake_client = _BenchmarkLlmClient()
        p = Pipeline(config, store, audit, llm_client=fake_client)

        # Stage 1 is already done (we seeded CRAWLED jobs). Just process.
        tracemalloc.start()
        t0 = time.perf_counter()
        res = p.process(
            "bench-0000",
            ts="2026-06-22T00:00:00Z",
            title="Benchmark title for performance test",
            ai_copy=False,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        results["process_single_ms"] = elapsed_ms
        results["process_single_state"] = res.final_state.value
        results["peak_memory_mb"] = round(peak_bytes / (1024 * 1024), 1)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="lcp performance benchmark")
    parser.add_argument(
        "--jobs", type=int, default=20, help="Number of synthetic jobs (default: 20)"
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    results = run_benchmark(num_jobs=args.jobs)
    results["tool"] = "lcp/benchmark"
    results["version"] = "0.1.0"

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"  lcp benchmark — {results['list_jobs_count']} jobs")
        print(f"  startup list:      {results['startup_list_ms']} ms")
        print(f"  list {results['list_jobs_count']} jobs:    {results['list_jobs_ms']} ms")
        print(f"  batch summary:     {results['batch_summary_ms']} ms")
        print(
            f"  process single:    {results['process_single_ms']} ms ({results['process_single_state']})"
        )
        print(f"  peak memory:       {results['peak_memory_mb']} MB")


if __name__ == "__main__":
    main()
