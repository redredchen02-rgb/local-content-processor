"""U6 (R5a lint) — ContentConfig.hype_words/min_copy_chars reach LintConfig.

`build_lint_config` previously dropped these two fields, so the rule's own
defaults always governed and the config knobs were dead. They are now forwarded
(empty/zero falls back to the calibrated default, so the projection stays
behavior-preserving until an operator tunes them).
"""

from __future__ import annotations

from lcp.adapters.processor.draft_linter import build_lint_config
from lcp.core.config import ContentConfig
from lcp.core.draft import Draft, FaqItem, MediaSection
from lcp.core.rules import lint_rules
from lcp.core.rules.lint_rules import DEFAULT_HYPE_WORDS, DEFAULT_MIN_COPY_CHARS

CATEGORIES = {"美食": []}


def _draft(**overrides) -> Draft:
    base = dict(
        title="台北週末美食市集盛大登場好熱鬧",
        intro="本週末在台北華山舉辦大型美食市集。",
        quick_facts=["時間：週六日", "地點：華山", "免費入場"],
        event_body=(
            "華山文創園區本週末舉辦美食市集。\n\n"
            "現場有上百個攤位提供各式小吃。\n\n"
            "主辦單位預估將吸引大量人潮。"
        ),
        image_sections=[MediaSection(asset_ref="img/a.jpg", caption="攤位實景")],
        faq=[FaqItem(question="需要門票嗎？", answer="不需要，免費入場。")],
        summary="這是一場不容錯過的週末活動。",
        tags=["美食", "市集", "華山"],
        keywords=["美食", "市集"],
        category="美食",
    )
    base.update(overrides)
    return Draft(**base)


def test_default_content_config_preserves_rule_defaults():
    """An untuned ContentConfig projects to the rule's own defaults (behavior
    unchanged from before the seam was wired)."""
    cfg = build_lint_config(ContentConfig(), {})
    assert cfg.hype_words == DEFAULT_HYPE_WORDS
    assert cfg.min_copy_chars == DEFAULT_MIN_COPY_CHARS


def test_operator_overrides_are_forwarded():
    """A tuned ContentConfig actually reaches LintConfig."""
    cfg = build_lint_config(ContentConfig(hype_words=["獨家", "勁爆"], min_copy_chars=99), {})
    assert cfg.hype_words == ("獨家", "勁爆")
    assert cfg.min_copy_chars == 99


def test_empty_or_zero_falls_back_to_default():
    """Empty list / 0 is 'unset' -> the calibrated default, never an accidental
    'no hype words' (which would silently disable the check)."""
    cfg = build_lint_config(ContentConfig(hype_words=[], min_copy_chars=0), {})
    assert cfg.hype_words == DEFAULT_HYPE_WORDS
    assert cfg.min_copy_chars == DEFAULT_MIN_COPY_CHARS


def test_custom_hype_word_changes_lint_verdict():
    """End-to-end: a custom hype word the operator added now trips the tag check.
    It is NOT in DEFAULT_HYPE_WORDS, so ONLY the wired config catches it (this
    fails against the unwired build_lint_config)."""
    assert "獨家" not in DEFAULT_HYPE_WORDS
    cfg = build_lint_config(ContentConfig(hype_words=["獨家"]), CATEGORIES)
    r = lint_rules.lint_draft(_draft(tags=["獨家", "美食", "市集"]), cfg)
    assert any("hype" in e or "獨家" in e for e in r.errors)


def test_custom_hype_word_objective_tags_still_pass():
    """The same custom-hype config does NOT flag objective tags (no false hit)."""
    cfg = build_lint_config(ContentConfig(hype_words=["獨家"]), CATEGORIES)
    r = lint_rules.lint_draft(_draft(tags=["美食", "市集", "華山"]), cfg)
    assert not any("hype" in e for e in r.errors)


def test_negative_min_copy_chars_falls_back_to_default():
    """A negative min_copy_chars (pydantic accepts it, no validator) is treated as
    unset -> the calibrated default, never a degenerate floor."""
    cfg = build_lint_config(ContentConfig(min_copy_chars=-5), {})
    assert cfg.min_copy_chars == DEFAULT_MIN_COPY_CHARS


# --- Unit 1: field-level lint tunables wiring --------------------------------


def test_unit1_intro_min_chars_forwarded():
    """ContentConfig(intro_min_chars=70) reaches LintConfig.intro_min_chars=70."""
    cfg = build_lint_config(ContentConfig(intro_min_chars=70), {})
    assert cfg.intro_min_chars == 70


def test_unit1_intro_min_chars_zero_uses_default():
    """ContentConfig(intro_min_chars=0) → LintConfig default (80)."""
    from lcp.core.rules.lint_rules import LintConfig as LC

    cfg = build_lint_config(ContentConfig(intro_min_chars=0), {})
    assert cfg.intro_min_chars == LC.__dataclass_fields__["intro_min_chars"].default


def test_unit1_all_new_fields_forwarded():
    """All 10 new tunables are forwarded correctly when explicitly set."""
    content = ContentConfig(
        intro_min_chars=70,
        intro_max_chars=130,
        event_body_min_chars=90,
        event_body_max_chars=250,
        summary_warn_chars=80,
        summary_error_chars=120,
        faq_min_count=2,
        faq_max_count=6,
        quick_facts_min_count=2,
        quick_facts_max_count=8,
    )
    cfg = build_lint_config(content, {})
    assert cfg.intro_min_chars == 70
    assert cfg.intro_max_chars == 130
    assert cfg.event_body_min_chars == 90
    assert cfg.event_body_max_chars == 250
    assert cfg.summary_warn_chars == 80
    assert cfg.summary_error_chars == 120
    assert cfg.faq_min_count == 2
    assert cfg.faq_max_count == 6
    assert cfg.quick_facts_min_count == 2
    assert cfg.quick_facts_max_count == 8


def test_unit1_zero_fields_use_lint_defaults():
    """All new tunables set to 0 fall back to LintConfig defaults."""
    from lcp.core.rules.lint_rules import LintConfig as LC

    cfg = build_lint_config(ContentConfig(), {})
    assert cfg.intro_min_chars == LC.__dataclass_fields__["intro_min_chars"].default
    assert cfg.intro_max_chars == LC.__dataclass_fields__["intro_max_chars"].default
    assert cfg.event_body_min_chars == LC.__dataclass_fields__["event_body_min_chars"].default
    assert cfg.event_body_max_chars == LC.__dataclass_fields__["event_body_max_chars"].default
    assert cfg.summary_warn_chars == LC.__dataclass_fields__["summary_warn_chars"].default
    assert cfg.summary_error_chars == LC.__dataclass_fields__["summary_error_chars"].default
    assert cfg.faq_min_count == LC.__dataclass_fields__["faq_min_count"].default
    assert cfg.faq_max_count == LC.__dataclass_fields__["faq_max_count"].default
    assert cfg.quick_facts_min_count == LC.__dataclass_fields__["quick_facts_min_count"].default
    assert cfg.quick_facts_max_count == LC.__dataclass_fields__["quick_facts_max_count"].default


def test_unit1_summary_warn_ge_error_raises():
    """summary_warn_chars >= summary_error_chars → InputValidationError."""
    from lcp.core.errors import InputValidationError

    # warn == error (degenerate — warn zone collapses to empty)
    try:
        build_lint_config(ContentConfig(summary_warn_chars=100, summary_error_chars=100), {})
        raise AssertionError("should have raised InputValidationError")
    except InputValidationError:
        pass

    # warn > error (inverted)
    try:
        build_lint_config(ContentConfig(summary_warn_chars=150, summary_error_chars=100), {})
        raise AssertionError("should have raised InputValidationError")
    except InputValidationError:
        pass


def test_unit1_summary_warn_lt_error_ok():
    """summary_warn_chars < summary_error_chars → valid, no exception."""
    cfg = build_lint_config(ContentConfig(summary_warn_chars=80, summary_error_chars=120), {})
    assert cfg.summary_warn_chars == 80
    assert cfg.summary_error_chars == 120
