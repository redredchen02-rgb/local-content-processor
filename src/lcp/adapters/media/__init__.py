"""Media I/O adapters: Pillow image normalization + ffprobe/ffmpeg probing.

All side effects (reading files off disk, spawning subprocesses) live here.
Pure threshold judgement is delegated to :mod:`lcp.core.rules.asset_rules`.

STATUS — WIRED INTO Stage 2: :func:`lcp.adapters.processor.media_checker.run_media_gate`
calls this subsystem inside :meth:`lcp.pipeline.Pipeline.process` (after the risk
hard-stop, before dedup/LLM). It normalizes images, composes the cover, probes
videos, writes processed/validation_report.json, and parks the job at
NEEDS_REVISION on any quality miss (never BLOCKED — that tier is risk-only). The
hardening tests here are load-bearing and must not be removed.
"""
