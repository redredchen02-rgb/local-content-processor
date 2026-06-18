"""Cross-language completeness guard for the GUI's pure-data UI layer (lex.js).

There is no JS test runner in this (pure-Python) project, so the #1 regression
risk — a new backend enum that the frontend forgets to translate, leaving the
operator staring at a raw `needs_human_review` token or a blank cell — has no
JS-side guard. This test closes that gap from Python.

It is a STRUCTURAL check, not a substring grep: it extracts the `LEX` and
`STATE_ACTIONS` object literals from `src/lcp/web/lex.js` (they are written as
valid JSON on purpose) and asserts that every JobState / ReviewReason / exit-code
has a real entry — a present KEY whose human text is non-empty AND not just the
raw enum token echoed back. A bare substring test would pass on
`{"new": "new"}`; this one fails it.
"""

import json
from pathlib import Path

from lcp.core.errors import (
    EXIT_DEPENDENCY,
    EXIT_EXTERNAL,
    EXIT_INPUT,
    EXIT_INTERNAL,
)
from lcp.core.state import JobState, ReviewReason

LEX_JS = Path(__file__).resolve().parents[1] / "src" / "lcp" / "web" / "lex.js"

# exit 1 (USAGE) is raised only in cli.py, never across the Api bridge, so the
# GUI runtime can only ever surface these four.
BRIDGE_EXIT_CODES = {EXIT_INPUT, EXIT_DEPENDENCY, EXIT_EXTERNAL, EXIT_INTERNAL}


def _extract(name: str, text: str) -> dict:
    """Pull a `const <name> = { ... };` JSON literal out of lex.js.

    The object is authored as valid JSON (double-quoted, no trailing commas, no
    inline comments) and is the only `\\n};`-terminated block for that name, so a
    plain split is robust."""
    body = text.split(f"const {name} = ", 1)[1].split("\n};", 1)[0] + "\n}"
    return json.loads(body)


def _lex() -> dict:
    return _extract("LEX", LEX_JS.read_text(encoding="utf-8"))


def _state_actions() -> dict:
    return _extract("STATE_ACTIONS", LEX_JS.read_text(encoding="utf-8"))


# --- LEX: every closed enum has a real, non-echo human entry -----------------


def test_lex_covers_every_jobstate_with_real_copy():
    lex_state = _lex()["state"]
    for st in JobState:  # all 16, including transient `processing`
        assert st.value in lex_state, f"lex.js LEX.state missing {st.value}"
        entry = lex_state[st.value]
        title = entry.get("title", "")
        assert title, f"{st.value} has empty title"
        assert title != st.value, f"{st.value} title echoes the raw enum token"
        assert entry.get("why"), f"{st.value} has empty why"


def test_lex_covers_every_review_reason():
    lex_reason = _lex()["reason"]
    for r in ReviewReason:
        assert r.value in lex_reason, f"lex.js LEX.reason missing {r.value}"
        entry = lex_reason[r.value]
        assert entry.get("label") and entry["label"] != r.value
        assert entry.get("why")


def test_lex_covers_bridge_exit_codes():
    lex_exit = _lex()["exit"]
    for code in BRIDGE_EXIT_CODES:
        assert str(code) in lex_exit, f"lex.js LEX.exit missing {code}"
        assert lex_exit[str(code)].get("title")


def test_lex_has_distinct_fallback_entries():
    lex = _lex()
    assert lex["fallback"]["state"].get("title")
    assert lex["fallback"]["exit"].get("title")
    # The fallback must not collide with a real enum key.
    assert "fallback" not in lex["state"]


def test_lex_honesty_has_dedup_disclaimer():
    """R36: dedup honesty copy lives in exactly one canonical place."""
    assert _lex()["honesty"].get("dedup")


# --- STATE_ACTIONS: legal-action sets are complete and fail-closed ------------


def test_state_actions_domain_equals_jobstate():
    sa = _state_actions()
    assert set(sa.keys()) == {s.value for s in JobState}


def test_terminal_states_have_zero_actions():
    """fail-closed: the truly-terminal states expose NO buttons (no
    'approve anyway')."""
    sa = _state_actions()
    for st in (
        JobState.REJECTED,
        JobState.SUPERSEDED,
        JobState.PUBLISHED_RECORDED,
    ):
        assert sa[st.value] == [], f"{st.value} must have no actions"


def test_blocked_duplicate_expose_only_recovery_actions():
    """U8: BLOCKED/DUPLICATE now expose a single OPERATOR-RECOVERY action that
    routes only to SUPERSEDED — never an 'approve anyway' / process / reopen
    button. BLOCKED uses the dedicated redline-override gesture (distinct from the
    plain single-step supersede); DUPLICATE uses the ordinary supersede."""
    sa = _state_actions()
    blocked_methods = [a["method"] for a in sa[JobState.BLOCKED.value]]
    dup_methods = [a["method"] for a in sa[JobState.DUPLICATE.value]]
    # Exactly the recovery action, nothing that could reopen the job.
    assert blocked_methods == ["supersedeRedline"]
    assert dup_methods == ["supersede"]
    # Anti-laundering at the UI layer too: no path back into the pipeline.
    for methods in (blocked_methods, dup_methods):
        assert "process" not in methods
        assert "approve" not in methods
    # The blocked dialog must NOT reuse the plain single-step supersede.
    assert "supersede" not in blocked_methods


def test_needs_revision_has_no_reject_action():
    """state.py has no NEEDS_REVISION->REJECTED edge; the button set must not
    smuggle a reject in (it would raise at runtime)."""
    methods = {a["method"] for a in _state_actions()[JobState.NEEDS_REVISION.value]}
    assert "reject" not in methods


def test_processing_is_actionless_progress_mode():
    assert _state_actions()[JobState.PROCESSING.value] == []
