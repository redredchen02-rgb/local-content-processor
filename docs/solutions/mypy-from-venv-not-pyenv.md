# Run mypy from .venv, never pyenv

**Problem.** Running `mypy` from a pyenv/system Python picks up a stale system
Pillow whose stubs disagree with the version CI installs. The result is
false-positive type errors (and sometimes false GREENS) that do not match CI —
you "fix" errors that do not exist or miss ones that do.

**Fix.** Always invoke the gate as `./.venv/bin/mypy`. The venv carries the
exact `.[crawl,media,llm,dedup,dev]` dependency set CI installs, so its result
is the one that decides the merge. Trust only that.

**Where.** CI (`.github/workflows/ci.yml`) runs bare `mypy` after installing the
venv deps; locally the only matching invocation is `./.venv/bin/mypy`. The
two-tier strict config lives in `pyproject.toml [tool.mypy]` (flags enumerated,
NOT `strict = true`, so the cli/gui shells stay non-strict).

**Tell-tale.** A type error that mentions `PIL`/`Pillow` or vanishes when you
switch Python interpreters — you are on the wrong interpreter, not looking at a
real error.
