#!/usr/bin/env python3
"""Release gate: assert a pushed git tag matches pyproject's declared version.

The package version is a static ``[project].version`` (manually bumped). This gate
makes a forgotten bump **fail loud** on the tag-triggered release path instead of
building and publishing a version that lies about its tag (PyPI is immutable — a
wrong first upload cannot be re-uploaded). Invoked by the ``release.yml`` build job.

Args (argv): the pushed tag (``GITHUB_REF_NAME``, expected as ``vX.Y.Z``), then an
optional pyproject path. Exits 0 when ``tag == "v" + version``, 1 on a mismatch, and
2 on a usage/parse error — all distinguishable so the workflow step fails closed.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def read_declared_version(pyproject: Path) -> str:
    """Return the non-empty ``[project].version`` string, or raise ``ValueError``."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"no non-empty [project].version in {pyproject}")
    return version


def check(tag: str, version: str) -> str | None:
    """Return ``None`` when the tag matches, else a human-readable mismatch message.

    The convention is a ``v``-prefixed tag (``v1.2.3``) for the bare ``1.2.3``
    version; a tag without the prefix is reported as a mismatch with the expected
    form, so the operator sees exactly what to push.
    """
    expected = f"v{version}"
    if tag == expected:
        return None
    return f"tag {tag!r} does not match pyproject version (expected {expected!r})"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_tag_matches_version.py <tag> [pyproject.toml]", file=sys.stderr)
        return 2
    tag = argv[0]
    pyproject = (
        Path(argv[1])
        if len(argv) > 1
        else Path(__file__).resolve().parent.parent / "pyproject.toml"
    )
    try:
        version = read_declared_version(pyproject)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    mismatch = check(tag, version)
    if mismatch is not None:
        print(f"ERROR: {mismatch}", file=sys.stderr)
        return 1
    print(f"OK: tag {tag} matches pyproject version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
