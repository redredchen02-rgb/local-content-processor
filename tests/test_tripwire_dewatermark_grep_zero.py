"""Tripwire: the de-watermark / inpaint engine must stay CUT (plan-003, U20).

The de-watermark + inpaint pipeline was deliberately removed on 2026-06-17 (it
was a content-laundering capability sourced from an NCII/doxxing site). This
test fails if any of its signature tokens reappear in the shipped code.

WHY a grep tripwire and not a code assertion: the engine is GONE — there is no
symbol left to assert against. The only durable guard is "this string must not
come back". A reviewer who reintroduces `onnxruntime` (the inpaint runtime) or
an `inpaint`/`dewatermark` symbol trips this immediately.

CONTRACT (load-bearing): the de-watermark engine and its mandatory two-person
attestation may only ever return TOGETHER. The capability is not "off by a
flag" — it is absent. Reintroducing the engine without the human-in-the-loop
attestation that gated it is exactly the regression this tripwire forbids.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# The forbidden engine signatures (case-insensitive). Kept as separate fragments
# joined at runtime so this very file does not contain the literal token and
# therefore cannot self-trip — see the EXCLUDED-paths note below.
_FORBIDDEN_FRAGMENTS = ("dewa" + "termark", "inp" + "aint", "onnx" + "runtime")
_FORBIDDEN_RE = re.compile("|".join(_FORBIDDEN_FRAGMENTS), re.IGNORECASE)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Scan ONLY these trees. tests/ and docs/ are EXCLUDED on purpose: the cut is
# described in docs/solutions and exercised by legacy-key tolerance tests, and
# this tripwire file names the tokens — none of those is a reintroduction.
_SCAN_ROOTS = ("src", "spikes")
_SCAN_FILES = ("pyproject.toml",)

# This test file's own literal is excluded so its forbidden-token strings (and
# the docstring above) never self-trip the scan.
_SELF = Path(__file__).resolve()


def _tracked_paths() -> list[Path]:
    """Git-tracked files under the scanned trees (untracked spikes/junk and the
    stale spikes/dewatermark/ cache dir are not part of the shipped code)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", *_SCAN_ROOTS, *_SCAN_FILES],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [(_REPO_ROOT / rel) for rel in out.split("\0") if rel]


def test_dewatermark_engine_stays_cut() -> None:
    offenders: list[str] = []
    for path in _tracked_paths():
        if path.resolve() == _SELF:
            continue  # never scan the tripwire's own literal
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary/unreadable tracked asset: no source tokens to find
        if _FORBIDDEN_RE.search(text):
            offenders.append(str(path.relative_to(_REPO_ROOT)))

    assert not offenders, (
        "de-watermark/inpaint engine tokens reappeared in the shipped code "
        f"({offenders}). The engine + its two-person attestation may only ever "
        "return TOGETHER; see docs/plans/2026-06-17-003 (the cut)."
    )


def test_scan_actually_covers_source() -> None:
    """Guard the guard: if the scan set were empty the tripwire would pass
    vacuously. Assert it really walks the source tree."""
    paths = {p.relative_to(_REPO_ROOT).as_posix() for p in _tracked_paths()}
    assert "pyproject.toml" in paths
    assert any(p.startswith("src/lcp/") for p in paths)
