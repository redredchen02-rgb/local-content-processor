# PyPI release runbook — cut a release, recover from failure (2026-06-22)

A copy-pasteable go/no-go for publishing the **Python package** to PyPI via the
fail-closed three-job chain in `.github/workflows/release.yml`
(`build → publish-pypi → github-release`). Every field name below is meant to
match that workflow exactly — keep them in sync.

> This runbook publishes the **Python package (the tool)**. It has nothing to do
> with *content* publishing, which remains human-only and is never automated —
> the machine still never writes content to a CMS. "Releasing `lcp` to PyPI" is
> orthogonal to the "no auto-publish of content" invariant.

Concrete values for this repo (substitute if forked):

| Field | Value |
|---|---|
| PyPI project name | `local-content-processor` |
| GitHub owner | `redredchen02-rgb` |
| GitHub repository | `local-content-processor` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |
| Default branch | `main` |
| Tag shape | `vX.Y.Z` (e.g. `v0.2.0`) |

## 0. One-time setup (without this, `publish-pypi` cannot succeed)

**a. Register the PyPI pending publisher.** On PyPI →
*Account → Publishing → Add a pending publisher* (GitHub). The fields must match
`release.yml` **exactly** — a single mismatch makes the OIDC mint fail with a
cryptic error at publish time:

| PyPI form field | Enter |
|---|---|
| PyPI Project Name | `local-content-processor` |
| Owner | `redredchen02-rgb` |
| Repository name | `local-content-processor` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

> **Name-reservation warning:** a *pending* publisher does **not** reserve the
> project name. Anyone can claim `local-content-processor` on PyPI until the
> first successful upload. Claim it early (the first real or rehearsal release).

**b. Create the GitHub Environment `pypi`.** Repo *Settings → Environments → New
environment → `pypi`*:

- **Required reviewers** (recommended for a compliance-first first release):
  add the maintainer(s). This is the human approval gate that fires *after* the
  build gates pass and *before* any PyPI contact.
- **Deployment branches and tags**: restrict to tags matching `v*` (so the
  environment's OIDC identity can only be assumed from a `v*` tag).

**c. Add a tag-protection ruleset** (*Settings → Rules → Rulesets → New tag
ruleset*): restrict who can create/push `v*` tags to maintainers. This is the
**enforced first boundary**; the build job's tag-is-ancestor-of-`main` check is
defense-in-depth, not the primary control.

**d. Note on action pinning.** Every action in `release.yml` is pinned to a
commit SHA (version in the trailing comment). Bumping a SHA — especially on
`pypa/gh-action-pypi-publish` (the job that holds `id-token: write`) — is a
**deliberate, reviewed** change, never an unattended dependabot auto-merge.

## 1. Per-release happy path (the go path)

```sh
# 1. Bump the single declared version (the one source of truth).
#    Edit pyproject.toml  [project]  version = "X.Y.Z"   (e.g. 0.2.0)

# 2. Move CHANGELOG.md [Unreleased] entries under a dated, NON-EMPTY section
#    header in this exact shape (the build gate matches "^## X.Y.Z"):
#        ## 0.2.0 — 2026-07-01
#    An empty section fails the build gate — this is a verified precondition.

# 3. Commit both to the default branch (via PR; the tag must be an ancestor of main).
git switch main && git pull
git add pyproject.toml CHANGELOG.md && git commit -m "release: v0.2.0"
git push origin main

# 4. Tag and push (only a maintainer can, per the tag ruleset).
git tag v0.2.0
git push origin v0.2.0
```

Then watch the run (`gh run watch` or the Actions tab):

1. **build** job runs every gate — tag-is-ancestor-of-`main`, `tag == "v"+version`,
   non-empty CHANGELOG section, `python -m build`, `twine check --strict`, the
   wheel-asset grep, and a fresh-venv `pip install dist/*.whl` smoke. Any ✗
   hard-stops here; **nothing has reached PyPI**.
2. **publish-pypi** pauses for the `pypi` environment **required reviewer**.
   Approve **only after this concrete checklist** — not a blind approve:
   - [ ] all `build` gates are green;
   - [ ] the version is the one you intended;
   - [ ] the tag is on `main` and was created by a maintainer.
   On approval the job re-hashes `dist/*`, fail-closed compares to the build
   digest, then OIDC-publishes (attestations on, no rebuild).
3. **github-release** (idempotent) extracts the CHANGELOG section and creates the
   GitHub Release with the artifacts attached.

Verify: the new version is live at <https://pypi.org/project/local-content-processor/>
and `pip install local-content-processor==0.2.0` works in a clean venv.

## 2. No-go / recovery table

`skip-existing` is **OFF** on the normal path so a forgotten bump fails loud — it
is used **only** for the documented retention-expiry recovery row below.

| Failure | Where | Recovery |
|---|---|---|
| `tag != "v"+version` | build, version-sync gate | bump `pyproject.version` (or the tag), delete the bad tag, re-tag. PyPI untouched. |
| Tag not an ancestor of `main` | build, ancestor gate | the tag was cut off-branch; re-create it on a `main` commit. PyPI untouched. |
| Empty / missing CHANGELOG section | build, CHANGELOG gate | add a non-empty `## X.Y.Z — <date>` section, re-tag. PyPI untouched. |
| `twine check` / wheel-grep / install smoke fail | build | fix the packaging defect, bump to a new patch, re-tag. PyPI untouched. |
| dist digest mismatch | publish-pypi | artifact was tampered/swapped across the job boundary — **do not approve again**; investigate, then re-run the whole tagged workflow. |
| **PyPI upload OK, `github-release` failed** | github-release | The version is **live and immutable** — do **NOT** re-tag or bump. Re-run **only** the idempotent `github-release` job **within the live run** (`gh run rerun <id> --job github-release`). If the artifact-retention window has expired or the run was deleted, re-run the whole tagged workflow with `skip-existing: true` for that one recovery (the PyPI step no-ops on the already-present version). |
| A published version is genuinely broken | post-publish | PyPI is immutable — **never** delete-and-reupload. `yank` the bad version on PyPI and cut a new patch `vX.Y.Z+1`. Re-tagging an existing version is never recovery. |

## 3. First-release rehearsal (optional, recommended once)

Rehearse the full chain against **TestPyPI** before the first real publish, using
a real `vX.Y.Z`-shaped tag so it exercises the version-sync assertion gate:

1. Register a TestPyPI pending publisher (same fields, on <https://test.pypi.org>).
2. Temporarily point the publish step at TestPyPI:
   `with: { repository-url: https://test.pypi.org/legacy/ }` on the
   `pypa/gh-action-pypi-publish` step.
3. Push a throwaway tag (e.g. `v0.0.1rc1` with a matching `pyproject.version`),
   approve the `pypi` environment, confirm the chain goes green end to end.
4. **Revert the `repository-url` override** before any real release.

## 4. Go / no-go checklist

- [ ] `pyproject.version` bumped to the intended `X.Y.Z`.
- [ ] CHANGELOG has a **non-empty** `## X.Y.Z — <date>` section.
- [ ] The release commit is on `main`; the `vX.Y.Z` tag is an ancestor of `main`
      and was created by a maintainer (tag ruleset enforced).
- [ ] All actions in `release.yml` are SHA-pinned (no moving tags).
- [ ] The `pypi` environment has the required reviewer set.
- [ ] PEP 740 attestations are on (the publish step never sets `attestations: false`).
- [ ] The pending publisher's five fields match `release.yml` exactly.
