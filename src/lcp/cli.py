"""CLI shell (imperative). Thin: parse args -> call core -> format output.

Business logic lives in core/adapters; this shell only reads error.exit_code
to decide the process exit status (plan R30, R31)."""

from __future__ import annotations

import sys

import click

from .core.errors import EXIT_INTERNAL, EXIT_OK, LcpError, UsageError
from .runtime_hardening import apply_hardening


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.option("--dry-run", is_flag=True, help="Do not mutate external systems")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option("--verbose", is_flag=True, help="Verbose logging")
@click.option("--quiet", is_flag=True, help="Suppress non-error output")
@click.option("--output-dir", default=None, help="Override storage base dir")
@click.pass_context
def cli(ctx, config_path, dry_run, as_json, verbose, quiet, output_dir):
    """local-content-processor (lcp): crawl -> process -> review packet."""
    ctx.ensure_object(dict)
    ctx.obj.update(
        config_path=config_path,
        dry_run=dry_run,
        as_json=as_json,
        verbose=verbose,
        quiet=quiet,
        output_dir=output_dir,
    )


@cli.command()
@click.option("--url", default=None)
@click.option("--input", "input_file", default=None, help="URL list file")
@click.pass_context
def crawl(ctx, url, input_file):
    """Stage 1: crawl URL(s) into a raw job bundle. (implemented in Unit 4)"""
    raise UsageError("crawl is not implemented yet (Unit 4)")


@cli.command()
@click.option("--dir", "directory", required=True)
@click.pass_context
def ingest(ctx, directory):
    """Stage 1: ingest a local material folder. (implemented in Unit 4)"""
    raise UsageError("ingest is not implemented yet (Unit 4)")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Maps LcpError -> exit_code; unexpected -> EXIT_INTERNAL."""
    apply_hardening()
    try:
        cli.main(args=argv, standalone_mode=False)
        return EXIT_OK
    except click.UsageError as e:
        click.echo(str(e), err=True)
        return 1
    except click.ClickException as e:
        e.show()
        return 1
    except LcpError as e:
        click.echo(f"error: {e}", err=True)
        return e.exit_code
    except click.exceptions.Abort:
        return 1
    except Exception as e:  # noqa: BLE001 - shell boundary
        click.echo(f"internal error: {e}", err=True)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
