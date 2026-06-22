"""Pure linter for operator-editable prompt templates (plan Unit 3).

UR6 introduces, for the FIRST time, an operator-authored string into the prompt
assembly path (today the prompt is a hardcoded constant). A template is therefore
treated as a CHECKED OBJECT: it can never reach the SYSTEM message and can never
rewrite the zero-capability / data-not-instructions / grounding constraints. This
module is the deterministic gate — it does NOT ask an LLM to judge safety.

Severity:
  * ``errors`` -> the template is REJECTED (cannot be saved or imported).
  * ``warnings`` -> saveable but flagged (e.g. an injection-phrase in otherwise
    inert copy).

Run on SAVE and on IMPORT of a shared template (the threat is the same).
No I/O, no URL parsing — pure string analysis (mirrors lint_rules / grounding).
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass, field

from ..text_sanitize import _is_hidden_codepoint

# Hard cap: a template is a short instruction shell, not a document.
MAX_TEMPLATE_CHARS = 4096

# Role / turn markers that must never appear in operator copy — they are how an
# attacker tries to forge a new conversation turn or a higher-authority block.
_ROLE_MARKERS: tuple[str, ...] = (
    "<|",
    "|>",
    "<s>",
    "</s>",
    "[inst]",
    "[/inst]",
    "system:",
    "assistant:",
    "developer:",
    "###system",
    "### system",
)

# The datamark token prefix the assembler uses to fence untrusted DATA. A
# template embedding it is trying to smuggle a fake delimiter.
_DATAMARK_PREFIX = "DATA_"

# Visible injection phrases — warned (not rejected): the layered shell already
# neutralises them, but they are worth surfacing to the operator.
_INJECTION_PHRASES: tuple[str, ...] = (
    "ignore previous",
    "ignore the above",
    "ignore all previous",
    "disregard the above",
    "disregard previous",
    "forget the above",
    "you are now",
    "new instructions",
    "override",
    "jailbreak",
    "无视上述",
    "忽略上述",
    "忽略以上",
    "忽略之前",
)

_FENCE_RE = re.compile(r"```|~~~")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


@dataclass(frozen=True)
class TemplateLintResult:
    """Outcome of linting one template. ``rejected`` iff there are errors."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.errors)

    @property
    def ok(self) -> bool:
        return not self.errors


def _has_homoglyph(text: str) -> bool:
    """True if any non-ASCII letter NFKC-folds to an ASCII letter — a classic
    way to spell a role marker (e.g. fullwidth/мathematical 'SYSTEM') past a
    naive substring check."""
    for ch in text:
        if ch.isascii():
            continue
        if not ch.isalpha():
            continue
        folded = unicodedata.normalize("NFKC", ch)
        if any(c in string.ascii_letters for c in folded):
            return True
    return False


def lint_template(template: str, allowed_slots: frozenset[str] | set[str]) -> TemplateLintResult:
    """Lint an operator template against the allowlist + injection-surface rules.

    ``allowed_slots`` is the set of named ``{slot}`` placeholders the renderer
    permits (see ``templates.ALLOWED_SLOTS``). Any other placeholder, or a
    placeholder that reaches attributes/indexing/conversion (``.``/``[``/``!``/
    ``:``), is rejected — that is how ``str.format_map`` is abused to read object
    internals."""
    errors: list[str] = []
    warnings: list[str] = []

    if not template or not template.strip():
        errors.append("template is empty")
        return TemplateLintResult(errors, warnings)

    if len(template) > MAX_TEMPLATE_CHARS:
        errors.append(f"template too long: {len(template)} > {MAX_TEMPLATE_CHARS} chars")

    # Invisible channels (zero-width / bidi / tag / PUA / control).
    if any(_is_hidden_codepoint(ch) for ch in template):
        errors.append("template contains hidden/zero-width/bidi codepoints")
    if _has_homoglyph(template):
        errors.append("template contains non-ASCII homoglyph letters (NFKC folds to ASCII)")

    lowered = template.lower()
    for marker in _ROLE_MARKERS:
        if marker in lowered:
            errors.append(f"template contains a role/turn marker: {marker!r}")
            break
    if _DATAMARK_PREFIX in template:
        errors.append("template contains the reserved DATA delimiter prefix")
    if _FENCE_RE.search(template):
        errors.append("template contains a code fence")

    # Placeholder allowlist + no attribute/index/conversion access.
    for raw in _PLACEHOLDER_RE.findall(template):
        field_name = raw.strip()
        if any(c in raw for c in (".", "[", "]", "!", ":")):
            errors.append(f"placeholder reaches attributes/format spec: {{{raw}}}")
            continue
        if field_name not in allowed_slots:
            errors.append(f"unknown placeholder: {{{field_name}}}")

    for phrase in _INJECTION_PHRASES:
        if phrase in lowered:
            warnings.append(f"possible injection phrase present: {phrase!r}")

    return TemplateLintResult(errors, warnings)
