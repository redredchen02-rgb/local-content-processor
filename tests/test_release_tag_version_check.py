"""U3: the release version-sync gate (`scripts/check_tag_matches_version.py`).

The single most important release gate — a forgotten version bump must FAIL the
tagged build before anything reaches immutable PyPI. Loaded by path (the script
lives outside the importable package, in top-level `scripts/`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_tag_matches_version.py"
_spec = importlib.util.spec_from_file_location("check_tag_matches_version", _SCRIPT)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def _write_pyproject(tmp_path: Path, version_line: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(f'[project]\nname = "x"\n{version_line}\n', encoding="utf-8")
    return p


def test_matching_tag_passes(tmp_path) -> None:
    py = _write_pyproject(tmp_path, 'version = "0.2.0"')
    assert checker.check("v0.2.0", "0.2.0") is None
    assert checker.main(["v0.2.0", str(py)]) == 0


def test_mismatched_tag_fails_and_names_both(tmp_path, capsys) -> None:
    py = _write_pyproject(tmp_path, 'version = "0.1.0"')
    msg = checker.check("v0.2.0", "0.1.0")
    assert msg is not None and "v0.2.0" in msg and "v0.1.0" in msg
    assert checker.main(["v0.2.0", str(py)]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_tag_without_v_prefix_fails_with_expected_form() -> None:
    msg = checker.check("0.2.0", "0.2.0")
    assert msg is not None and "v0.2.0" in msg


def test_missing_project_version_fails_loud(tmp_path) -> None:
    # A pyproject with no [project].version must not silently pass.
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert checker.main(["v0.2.0", str(py)]) == 2


def test_no_args_is_usage_error() -> None:
    assert checker.main([]) == 2


def test_malformed_toml_fails_loud(tmp_path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text("this is : not = valid toml [[[", encoding="utf-8")
    assert checker.main(["v0.2.0", str(py)]) == 2


def test_repo_pyproject_matches_its_own_version() -> None:
    """Sanity: the committed pyproject's declared version is read cleanly and a
    v-tag of it would pass (guards against a future malformed bump)."""
    repo_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    version = checker.read_declared_version(repo_pyproject)
    assert checker.check(f"v{version}", version) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
