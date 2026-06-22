"""Local Content Processor (lcp): crawl -> process -> review packet (MVP)."""

# Single source of truth: the version is read from the installed package metadata
# (which setuptools fills from pyproject's [project].version), not hand-maintained
# here — so the two can never drift. Imported under private names to keep them out
# of the `lcp.*` namespace (no_implicit_reexport is on for the strict modules).
from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__: str = _version("local-content-processor")
except _PackageNotFoundError:  # running from an uninstalled source tree
    __version__ = "0.0.0+unknown"
