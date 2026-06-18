# Over-clean fixtures let unit tests mask integration bugs

**Problem.** PR #5's leaf tests were all green, yet the real pipeline path was
broken. The fixtures were too clean: a passthrough `urljoin` stub, manifests
and ffprobe values that were always well-formed. Each leaf passed in isolation
because the stub never produced the malformed input the real collaborator
produces — so the gate that should have fail-closed was never exercised against
a bad value.

**Fix.** For each feature-bearing change, add ONE test that drives the real
pipeline path (real collaborator, or a realistically *malformed* input — a
relative URL, a truncated manifest, a non-numeric ffprobe field), not just the
happy-path leaf. Reserve the heavier templates (subprocess tests for the crawler
seam, barrier-driven N-writer tests for the WAL writes) for the units that
actually have those seams.

**Where.** This whole stabilization effort (plan-001 U1–U15): the fixes were
findable only by replacing the clean stub with the real collaborator. See
`docs/2026-06-17-content-pipeline-upgrade-PR5-review-guide.md` for the original
lesson.

**Tell-tale.** A bug that ships despite a green suite, found only by running the
app end-to-end — your fixtures are cleaner than reality. Scope the integration
test to the exact leaf-vs-pipeline gap; do not blanket-integration-test
everything (CI cost / flakiness).
