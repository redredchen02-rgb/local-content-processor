# The real happy path was unreachable — and green tests hid it

**Problem.** The pipeline's headline outcome (produce a review packet) could
**never** happen through the real Stage-2 gate chain: three lint-required draft
fields — `tags`, `quick_facts`, `summary` — had **no producer** in `process()`
(`assemble` inited them empty and was never passed `tags`; the copywriter filled
only captions/FAQ/subheads/titles), and `image_sections` was unconditionally
required but filled solely by LLM captions. Every real run dead-ended at
`NEEDS_REVISION`. Yet **~751 tests were green** — because *every* test that
reached `PROCESSED` did so out-of-band via `persist_gate_state(...)`, and the one
test that ran the real chain asserted a *disjunction*
(`final_state in (PROCESSED, NEEDS_HUMAN_REVIEW, NEEDS_REVISION)`) — green even
though `PROCESSED` was impossible.

**Why it hid.** A shortcut that sets the *resting state* directly (here
`persist_gate_state`) lets unit tests exercise everything *after* a gate without
ever proving the gate's own output can satisfy the *next* gate. The wiring
between producer (assemble/copywriter) and consumer (lint/grounding) is exactly
what no leaf test covered. Same class as the PR #5 cover-watermark bug
(`make_cover` dropped a kwarg; unit tests stayed green; only an e2e caught it) —
see [[unit-tests-mask-integration-bugs]].

**Fix.**
1. Generate the missing sections in the copywriter under the grounding contract
   (`quick_facts`/`summary` join `_split_claims`; `tags` are trimmed-to-5 +
   hype-stripped at parse). Make `image_sections` conditional on `has_images`
   (mirroring `video_sections`), so legitimate text-only articles publish.
2. Add **one durable e2e test that reaches `PROCESSED` through the real gates**
   (`tests/test_e2e_pipeline.py`) — and that **must not** use `persist_gate_state`
   to get there. That single test is the standing guard.

**Tell-tale to design against.** A "happy path passes" assertion that is really a
disjunction including the *failure* states. If `assert state in (GOOD, BAD1,
BAD2)`, you have not tested the good path — pin `assert state is GOOD` in at least
one test that runs the real chain end to end.

**Fixture caveat.** To pass grounding without tripping the copied-too-much lint,
keep source paragraphs `<40` chars and ground the body via the 0.6 bigram-overlap
path rather than authoring every claim verbatim — a verbatim-everywhere source is
itself the over-clean-fixture failure mode this guards against.
