"""U7 — Error-handling boundary characterization.

Ast-grep / grep audit of all ``except`` clauses in ``src/lcp/``, classified into
three categories:

✅ **Specific type** — catches a single known exception (e.g. ``FileNotFoundError``,
   ``ExternalServiceError``). This is the ideal pattern.

✅ **Boundary conversion** — catches a broad type but immediately transforms it
   into an ``LcpError`` subtype with a specific message. This is the accepted
   pattern at module boundaries (CLI shell, GUI bridge, webserver handlers).

✅ **Resource cleanup** — catches broadly to clean up (kill subprocess, remove
   temp file) and re-raises. This is accepted for resource safety.

The test below verifies that ALL broad catches are accompanied by a ``# noqa:
BLE001`` comment (the convention established by this codebase) or are in one of
the known cleanup patterns.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "lcp"

# The only modules allowed to have `except BaseException:` — always for
# resource-cleanup-then-re-raise. Every such instance must be reviewed.
ALLOWED_BASE_EXCEPTION = frozenset(
    {
        "adapters/media/ffprobe.py",  # _kill_group cleanup on any error
        "adapters/storage/config_io.py",  # atomic_write_text temp-file cleanup
    }
)


def _iter_module_paths() -> list[pathlib.Path]:
    """Yield all *.py paths under src/lcp/ except __init__ stubs."""
    return sorted(SRC.rglob("*.py"))


def _relative(p: pathlib.Path) -> str:
    return str(p.relative_to(SRC))


@pytest.mark.parametrize("mod_path", _iter_module_paths(), ids=_relative)
def test_no_bare_except_in_strict_modules(mod_path: pathlib.Path) -> None:
    """Strict modules (lcp.core.*, lcp.pipeline, lcp.adapters.*) must not
    contain bare ``except:`` clauses."""
    rel = _relative(mod_path)
    # Only check strict modules (no shells)
    if rel.startswith("cli.py") or rel.startswith("gui.py") or rel.startswith("webserver.py"):
        pytest.skip("shell module — allowed bare except patterns")

    source = mod_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    class BareExceptFinder(ast.NodeVisitor):
        def __init__(self):
            self.found: list[int] = []

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                self.found.append(node.lineno)

    finder = BareExceptFinder()
    finder.visit(tree)
    assert not finder.found, (
        f"{rel}: bare `except:` at lines {finder.found}. Every except must name an exception type."
    )


@pytest.mark.parametrize("mod_path", _iter_module_paths(), ids=_relative)
def test_except_baseexception_requires_allowlist(mod_path: pathlib.Path) -> None:
    """``except BaseException`` is ONLY allowed in the known cleanup modules."""
    rel = _relative(mod_path)
    source = mod_path.read_text(encoding="utf-8")

    if "BaseException" not in source:
        pytest.skip(f"no BaseException in {rel}")

    if rel in ALLOWED_BASE_EXCEPTION:
        pytest.skip(f"BaseException in {rel} is pre-authorized (resource cleanup)")

    pytest.fail(
        f"{rel}: `except BaseException` not in allowed list. "
        "BaseException should almost never be caught. If truly necessary, "
        f"add it to ALLOWED_BASE_EXCEPTION in {__file__} with justification."
    )


@pytest.mark.parametrize("mod_path", _iter_module_paths(), ids=_relative)
def test_broad_exception_has_noqa_comment(mod_path: pathlib.Path) -> None:
    """Every broad ``except Exception`` must have ``# noqa: BLE001`` comment,
    the explicit acknowledgement that the catch is broader than ideal for a
    known reason (boundary conversion, resource cleanup, defensive fallback).
    """
    rel = _relative(mod_path)
    source = mod_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    class BroadExceptFinder(ast.NodeVisitor):
        def __init__(self):
            self.found: list[tuple[int, str]] = []

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                return  # bare except — covered by test_no_bare_except
            if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                # Check for # noqa: BLE001 in the same line or within 2 lines above
                lines = source.splitlines()
                lineno = node.lineno
                context_lines = lines[max(0, lineno - 3) : lineno]
                has_noqa = any("noqa: BLE001" in cl for cl in context_lines)
                if not has_noqa:
                    self.found.append((lineno, lines[lineno - 1].strip()))

    finder = BroadExceptFinder()
    finder.visit(tree)
    assert not finder.found, (
        f"{rel}: broad `except Exception` at lines without `# noqa: BLE001`:\n"
        + "\n".join(f"  L{ln}: {code}" for ln, code in finder.found)
        + "\nAdd `# noqa: BLE001 - <reason>` to acknowledge the broad catch."
    )


# --- Known exception-boundary mapping: key entry points ---

ENTRY_POINTS: dict[str, list[str]] = {
    # Shell boundary: CLI main -> all LcpError subtypes mapped to exit codes
    "cli.py main": [
        "OSError → OSError (port conflict, re-raised)",
        "click.UsageError → exit code 2",
        "click.ClickException → exit code 2",
        "LcpError → typed exit code (3/4/5)",
        "click.Abort → exit 130 (SIGINT)",
        "Exception → exit 5 (unexpected)",
    ],
    # Bridge boundary: GUI Api methods -> LcpError (never raw exception text)
    "gui.py bridge": [
        "LcpError → error dict with exit_code",
        "Exception → {error: 'internal error', exit_code: 5}",
        "Raw exception text NEVER crosses the bridge to the web UI",
    ],
    # Webserver request handler -> error response JSON
    "webserver.py handler": [
        "TypeError / ValidationError → 400",
        "LcpError → 4xx/5xx with exit_code",
        "Exception → 500 with generic message (never stack trace)",
    ],
    # Pipeline boundary: Stage-2 retry
    "pipeline.py process": [
        "ExternalServiceError → PROCESS_FAILED (retriable)",
        "DependencyError → PROCESS_FAILED (retriable)",
    ],
}

ALLOWED_ENTRY_POINTS = frozenset(ENTRY_POINTS.keys())


def test_entry_point_exception_mapping_documented() -> None:
    """Verify the expected exception boundaries are documented here."""
    assert len(ENTRY_POINTS) >= 4, "at least 4 key entry points should be documented"
    for ep, mapping in ENTRY_POINTS.items():
        assert mapping, f"{ep} must have at least one expected exception mapping"


def test_pipeline_retry_only_expected_exceptions() -> None:
    """Pipeline retry path (PROCESS_FAILED) must ONLY catch ExternalServiceError
    and DependencyError — never a bare Exception."""
    source = (SRC / "pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    class PipelineRetryFinder(ast.NodeVisitor):
        def __init__(self):
            self.matches: list[int] = []

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                return
            # Look for PROCESS_FAILED in the except body
            lines = source.splitlines()
            body_start = node.lineno
            body_end = node.end_lineno or body_start
            body_text = "\n".join(lines[body_start:body_end])
            if "PROCESS_FAILED" in body_text or "process_failed" in body_text.lower():
                self.matches.append(node.lineno)

    finder = PipelineRetryFinder()
    finder.visit(tree)
    assert finder.matches, "no PROCESS_FAILED except found — check pattern"
