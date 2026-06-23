# Release: version↔tag single source + fail-closed three-job chain

**Problem.** Two coupled release footguns on an *immutable* publishing target
(PyPI rejects a re-uploaded version; a wrong first upload is forever). (1) The
package version lived as a hardcoded literal in *two* places (`pyproject.toml`
and `src/lcp/__init__.py`), and `python -m build` baked it regardless of the
pushed git tag — so the second release would either re-upload the old version or
ship a version that lies about its tag. (2) A single release job held both
`id-token: write` (OIDC publish) and `contents: write` (GitHub Release) across
every step, gated only by a web-asset `grep`. A broken README, an un-importable
wheel, an empty CHANGELOG, or a tag pushed on an unreviewed off-branch commit
would all sail straight to the immutable upload and only be caught afterward.

**Fix.** Single-source the runtime version from installed metadata
(`importlib.metadata.version("local-content-processor")`, `try/except
PackageNotFoundError` → `"0.0.0+unknown"`); `pyproject.version` stays the one
declared string. Guard the tag path with an **assertion gate** — a tested,
type-checked `scripts/check_tag_matches_version.py` (stdlib `tomllib`) that fails
the build loud when `tag != "v"+pyproject.version`. Split the release into a
**fail-closed, concurrency-serialized three-job chain**:
`build → publish-pypi → github-release`, decisive-first, any ✗ hard-stops:

- **build** (no privilege; runs on PR + tag): tag-only gates
  (tag-is-ancestor-of-`main` via explicit `git fetch origin main` then
  `merge-base --is-ancestor`; version-sync; non-empty `^## X.Y.Z` CHANGELOG
  section) → `python -m build` → `twine check --strict` → wheel-asset grep →
  **real fresh-venv `pip install dist/*.whl` smoke** → record a `dist/*` SHA256
  job output.
- **publish-pypi** (`id-token: write` only; `environment: pypi` human gate; runs
  **no first-party code**): download artifact → **re-hash and fail-closed compare
  to the build digest** → OIDC publish (attestations on, never rebuilds).
- **github-release** (`contents: write` only; idempotent) ordered **last** so a
  Release flake never strands a successful immutable upload.

**Where.** `src/lcp/__init__.py` (metadata read, no in-tree consumers — verified
low-blast-radius), `scripts/check_tag_matches_version.py` +
`tests/test_release_tag_version_check.py` (typed via `scripts/` added to mypy
`files`), `.github/workflows/release.yml` (the chain). Plan
`docs/plans/2026-06-22-005-feat-productionization-finish-plan.md` U1/U3;
operator procedure in `docs/2026-06-22-pypi-release-runbook.md`.

**Scope (do not overstate).** The assertion gate is a **CI-path control on the
tag-triggered build job**, not a property of the artifact: a manual/local
`python -m build` or any publish path that skips the tag-triggered job bypasses
it. "Fail loud on a forgotten bump" holds for the tag-triggered path only — the
runbook routes maintainers exclusively through it.

**Tell-tale.** A version literal duplicated anywhere outside `pyproject.version`;
`id-token: write` or `contents: write` sitting on a job that doesn't strictly
need it; a release gate softened with `|| true` (silently defeats it); the
immutable PyPI step ordered *after* a cosmetic step that could strand it; or the
CHANGELOG matcher falling back to `[Unreleased]` instead of failing closed.
