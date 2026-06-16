"""Media I/O adapters: Pillow image normalization + ffprobe/ffmpeg probing.

All side effects (reading files off disk, spawning subprocesses) live here.
Pure threshold judgement is delegated to :mod:`lcp.core.rules.asset_rules`.

STATUS — STAGED, NOT YET WIRED INTO Stage 2: this subsystem (image normalizer,
ffprobe spec/black/silence checks, decompression-bomb + process-group-timeout
guards) is implemented and unit-tested, but :meth:`lcp.pipeline.Pipeline.process`
does NOT yet call it — Stage 2 currently runs risk -> dedup -> assemble -> lint +
grounding only. Wiring it in is the remaining step; until then, do not read the
Stage-2 docstrings as implying media validation runs. (The hardening tests here
are load-bearing and must not be removed.)
"""
