"""Per-栏目 prompt-template management + safe render (plan Unit 3).

Operators maintain a reusable instruction shell per category (今日吃瓜 /
网红黑料 / …). A template is a CHECKED OBJECT:

  * it is linted on SAVE and on IMPORT (``template_lint.lint_template``) — a
    rejected template is never stored or used;
  * it renders ONLY a fixed allowlist of named slots via ``str.format_map`` (NOT
    Jinja2 — its sandbox is anti-RCE, not anti-injection); an unknown placeholder
    or any attribute/index/format-spec access raises;
  * the rendered text goes into the DEVELOPER task slot of the user-facing
    message, framed as "a request, not authority" — it NEVER reaches the
    hardcoded SYSTEM message that holds the zero-capability / grounding /
    anti-injection constraints.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from lcp.core.config import Config
from lcp.core.errors import InputValidationError
from lcp.core.rules.template_lint import TemplateLintResult, lint_template
from lcp.core.text_sanitize import sanitize_source

# The named slots an operator template may reference. Deliberately small: these
# are framing values, not a channel to inject source text or instructions.
ALLOWED_SLOTS: frozenset[str] = frozenset({"category", "title", "tags", "keywords"})


class _AllowlistMapping(Mapping[str, str]):
    """A format_map mapping that yields only allowlisted slots and rejects any
    other key (so ``str.format_map`` cannot reach unexpected names)."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> str:
        if key not in ALLOWED_SLOTS:
            raise InputValidationError(f"template uses a non-allowlisted slot: {{{key}}}")
        return self._values.get(key, "")

    def __iter__(self) -> Iterator[str]:  # pragma: no cover - not used by format_map
        return iter(self._values)

    def __len__(self) -> int:  # pragma: no cover - not used by format_map
        return len(self._values)


def validate_template(template: str) -> TemplateLintResult:
    """Lint a template; raise InputValidationError if it is rejected. Returns the
    result (with any warnings) when accepted. Call on save AND on import."""
    result = lint_template(template, ALLOWED_SLOTS)
    if result.rejected:
        raise InputValidationError("template rejected: " + "; ".join(result.errors))
    return result


def render_template(template: str, values: dict[str, str]) -> str:
    """Render an ALREADY-VALIDATED template with allowlisted slot values.

    Re-validates defensively (cheap, deterministic) so a render path can never
    be reached with an unchecked template. Unknown placeholders / attribute
    access raise InputValidationError, never leak object internals."""
    validate_template(template)
    # Slot VALUES are untrusted (e.g. {title} is routinely lifted from a scraped
    # headline). The allowlist bounds slot KEYS, not VALUES — so datamark/escape
    # each value the same way USER source is sanitized before the LLM: strip
    # zero-width / bidi / tag / control codepoints so a value cannot smuggle an
    # invisible fence-break or hidden instruction into the (subordinate) developer
    # block. Visible text stays neutralised by build_developer_block's "request,
    # not authority" framing + the zero-capability LLM + needs_human_review.
    safe_values = {k: sanitize_source(v) for k, v in values.items()}
    try:
        return template.format_map(_AllowlistMapping(safe_values))
    except (KeyError, IndexError, AttributeError, ValueError) as e:
        raise InputValidationError(f"template render failed: {e}") from e


def get_template(config: Config, category: str | None) -> str | None:
    """Return the (validated) template for a category, or None if none is set."""
    if not category:
        return None
    template = config.templates.get(category)
    if template is None:
        return None
    validate_template(template)
    return template


def list_template_categories(config: Config) -> list[str]:
    """Categories that have a template configured (for the GUI/CLI picker)."""
    return sorted(config.templates.keys())
