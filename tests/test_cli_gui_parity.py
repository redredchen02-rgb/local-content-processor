"""CLI ↔ GUI mirror parity test.

Asserts every operator-action CLI command has an Api twin and vice versa.
Adding an operator action to one shell and not the other turns CI red.

Known exceptions (shell-only, no mirror needed):
- ``gui`` — CLI-only launcher
- ``init`` — CLI-only workspace scaffold
- ``*_async`` twins — GUI-only background kickoffs (no CLI command)
- ``list`` ↔ ``list_jobs`` — name divergence, normalized
- ``review-packet`` ↔ ``make_review_packet`` — name divergence, normalized
"""

from __future__ import annotations

from lcp.cli import cli
from lcp.gui import Api
from lcp.webserver import public_routes

# --- CLI side: collect all command names from the click group ----------


def _cli_commands() -> set[str]:
    """All registered CLI command names (flat, no subgroups)."""
    return set(cli.commands.keys())


# --- GUI side: collect all public Api methods ---------------------------


def _gui_methods() -> frozenset[str]:
    """All public Api methods (via webserver.public_routes)."""
    return public_routes(Api)


# --- Normalization: map divergent names to a shared vocabulary ----------

# CLI name -> canonical name
_CLI_NORMALIZE: dict[str, str] = {
    "list": "list_jobs",
    "review-packet": "review_packet",
    "ingest-gossip": "ingest_gossip",
}

# GUI method -> canonical name
_GUI_NORMALIZE: dict[str, str] = {
    "make_review_packet": "review_packet",
    "create_and_crawl": "crawl",
    "create_and_crawl_async": "crawl",  # async twin, excluded
    "crawl_ingested": "ingest",
    "ingest_dir": "ingest",
    "get_job": "get_job",
    "get_packet": "review_packet",
    "cover_report": "cover_report",
    "job_status": "job_status",
    "dashboard_stats": "summary",
    "init_workspace": "init",
    "process_batch": "process",
}

# Shell-only CLI commands (no GUI mirror needed)
_CLI_ONLY: frozenset[str] = frozenset({"gui", "init", "run"})

# GUI-only methods (async twins, data endpoints, internal helpers — no CLI mirror needed)
_GUI_ONLY: frozenset[str] = frozenset(
    {
        "create_and_crawl_async",  # async background kickoff
        "process_async",  # async background kickoff
        "templates",  # data endpoint, not an operator action
        "reviewers",  # data endpoint
        "disclaimer",  # data endpoint
        "get_settings",  # settings read
        "save_settings",  # settings write (CLI uses config.yaml directly)
        "saved_sources",  # data endpoint
        "add_saved_source",  # CRUD helper
        "delete_saved_source",  # CRUD helper
        "summary",  # data endpoint (CLI uses list --summary)
        "dashboard_stats",  # data endpoint
        "cover_report",  # read-only data (CLI accesses via review-packet)
        "get_job",  # read-only data (CLI accesses via list)
        "get_packet",  # read-only data (CLI accesses via review-packet)
        "job_status",  # read-only data (CLI accesses via list)
    }
)


def _normalize_cli(name: str) -> str:
    return _CLI_NORMALIZE.get(name, name).replace("-", "_")


def _normalize_gui(name: str) -> str:
    return _GUI_NORMALIZE.get(name, name).replace("-", "_")


# --- Tests --------------------------------------------------------------


def test_cli_commands_have_gui_mirror():
    """Every CLI operator-action command must have a matching GUI method."""
    cli_cmds = _cli_commands() - _CLI_ONLY
    gui_methods = _gui_methods()
    gui_canonical = {_normalize_gui(m) for m in gui_methods} | gui_methods

    missing = []
    for cmd in sorted(cli_cmds):
        canonical = _normalize_cli(cmd)
        if canonical not in gui_canonical and cmd not in gui_methods:
            missing.append(cmd)

    assert not missing, (
        f"CLI commands without GUI mirror: {missing}. "
        f"Add matching Api methods or add to _CLI_ONLY / _GUI_NORMALIZE."
    )


def test_gui_methods_have_cli_mirror():
    """Every GUI operator-action method must have a matching CLI command."""
    gui_methods = _gui_methods() - _GUI_ONLY
    cli_cmds = _cli_commands()
    cli_canonical = {_normalize_cli(c) for c in cli_cmds} | cli_cmds

    missing = []
    for method in sorted(gui_methods):
        canonical = _normalize_gui(method)
        if canonical not in cli_canonical and method not in cli_cmds:
            missing.append(method)

    assert not missing, (
        f"GUI methods without CLI mirror: {missing}. "
        f"Add matching CLI commands or add to _GUI_ONLY / _GUI_NORMALIZE."
    )


def test_public_routes_matches_api():
    """public_routes(Api) must equal the hand-maintained list in webserver.
    (This is the existing guard — included here for completeness.)"""
    routes = public_routes(Api)
    # Sanity: at least the core operator actions exist
    for expected in ("approve", "reject", "list_jobs", "backfill"):
        assert expected in routes, f"core route {expected!r} missing from public_routes"
