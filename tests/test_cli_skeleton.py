from lcp.cli import main
from lcp.core.errors import EXIT_OK, EXIT_USAGE


def test_help_lists_commands(capsys):
    rc = main(["--help"])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "crawl" in out and "ingest" in out


def test_unimplemented_command_returns_usage_exit(capsys):
    rc = main(["crawl", "--url", "https://example.com/p/1"])
    assert rc == EXIT_USAGE


def test_no_command_shows_help_like_behaviour(capsys):
    rc = main([])
    # click group with no subcommand returns non-zero usage, never crashes
    assert rc in (EXIT_OK, EXIT_USAGE)
