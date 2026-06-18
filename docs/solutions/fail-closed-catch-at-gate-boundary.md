# Fail closed: each gate catches BEFORE the pipeline boundary

**Problem.** `Pipeline.process`'s outer `except` is intentionally
`ExternalServiceError`-only (LLM 5xx/timeout → `PROCESS_FAILED`, retriable). If a
Stage-2 gate lets a *foreseeable* bad input escape as a raw exception
(`ValueError` from a malformed media URL, a `JSONDecodeError` from a torn
manifest, a non-numeric ffprobe field), it sails past that narrow `except`,
becomes an uncaught crash, and the CLI maps it to exit 5 (unexpected) — the job
is left at `CRAWLED` instead of parked for a human. That is fail-OPEN: the
unsafe input produced a crash, not a hold.

**Fix.** Each gate handles its own foreseeable failures BEFORE the boundary and
parks the job at a hold state (`BLOCKED` / `DUPLICATE` / `NEEDS_HUMAN_REVIEW` /
`NEEDS_REVISION`) — a human decision, never an auto-pass. Only genuinely
external/transient faults are allowed to surface as `ExternalServiceError` and
hit the retriable path. A foreseeable bad input must become a parked state or a
typed `LcpError` (exit 2/3/4), never exit 5.

**Where.** `src/lcp/pipeline.py::_process_inner` runs the gates in fixed order
(`risk → media → dedup → assemble(LLM) → lint+grounding`) and stops at the first
that parks. The narrow boundary `except ExternalServiceError` is at
`pipeline.py:321`. The gate-level fail-closed fixes are plan-001 U1
(malformed media URL), U2 (corrupt site index), U5 (untrusted ffprobe numerics),
U6 (subprocess/manifest faults).

**Tell-tale.** A bad input that crashes (exit 5) instead of parking the job, or
a `try/except` at the pipeline boundary getting widened to "fix" a crash — widen
the GATE's handling instead, and keep the boundary `except` narrow.
