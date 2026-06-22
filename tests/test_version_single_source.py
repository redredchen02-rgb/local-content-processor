"""U1: `lcp.__version__` is sourced from installed metadata, not a hardcoded literal.

The version must track the one declared string in pyproject (`[project].version`) so
`pyproject.toml` and `src/lcp/__init__.py` can never drift, and importing from an
uninstalled tree must fall back rather than raise.
"""

import importlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

import lcp


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_version_matches_installed_metadata() -> None:
    assert lcp.__version__ == importlib.metadata.version("local-content-processor")


def test_version_matches_pyproject_declared_version() -> None:
    # On a fresh/editable install the metadata is regenerated from pyproject, so the
    # single declared string is what `lcp.__version__` resolves to.
    assert lcp.__version__ == _pyproject_version()


def test_version_is_pep440_like() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", lcp.__version__) is not None


def test_falls_back_when_package_metadata_missing(monkeypatch) -> None:
    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    try:
        reloaded = importlib.reload(lcp)
        assert reloaded.__version__ == "0.0.0+unknown"
    finally:
        # Restore the real metadata lookup and module state for the rest of the suite.
        monkeypatch.undo()
        importlib.reload(lcp)
