"""U6 (R5b) — grounding holds NO cross-job state.

`_char_shingles` was decorated with a module-global ``@lru_cache`` keyed on the
raw source/claim TEXT, so a PII-bearing source lingered in process memory across
jobs. The fix removes that cache and instead shingles the cleaned source ONCE
per ``verify_grounding`` call, reused across claims. These tests pin: (a) no
module-global cache remains, (b) verdicts are unchanged, (c) an injected
non-baseline strategy still goes through the plain Protocol seam.
"""

from __future__ import annotations

from lcp.core.draft import Draft, FaqItem, SourceQuote
from lcp.core.rules import grounding
from lcp.core.rules.grounding import (
    GroundingStatus,
    SubstringOverlapStrategy,
    verify_grounding,
)

SOURCE = (
    "華山文創園區本週末舉辦美食市集。\n"
    "現場有上百個攤位提供各式小吃與飲料。\n"
    "主辦單位預估將吸引大量人潮前往參觀。"
)


def test_no_module_global_shingle_cache():
    """The lru_cache is gone — `_char_shingles` must not expose cache_info /
    cache_clear, i.e. it retains no source/claim text across calls (jobs)."""
    assert not hasattr(grounding._char_shingles, "cache_info")
    assert not hasattr(grounding._char_shingles, "cache_clear")


def test_baseline_verdicts_unchanged_multi_claim():
    """The precompute-source-once path produces the SAME verdict as before: a
    draft with a mix of grounded and ungrounded claims routes to human, and the
    ungrounded one is reported."""
    d = Draft(
        title="美食市集",
        event_body="華山文創園區本週末舉辦美食市集。現場有上百個攤位提供各式小吃與飲料。",
        faq=[FaqItem(question="有什麼？", answer="主辦單位涉嫌收受廠商鉅額回扣遭約談")],  # absent
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    r = verify_grounding(d, SOURCE)
    assert r.status == GroundingStatus.NEEDS_HUMAN_REVIEW
    assert any(u.kind == "claim" for u in r.ungrounded_claims)


def test_fully_grounded_draft_still_passes():
    """A draft whose claims are all source substrings passes (precompute path
    must not introduce false 'ungrounded')."""
    d = Draft(
        event_body="華山文創園區本週末舉辦美食市集。現場有上百個攤位提供各式小吃與飲料。",
        quotes=[SourceQuote(text="華山文創園區本週末舉辦美食市集。")],
    )
    assert verify_grounding(d, SOURCE).passed


def test_repeated_calls_isolated():
    """Two verify_grounding calls with DIFFERENT sources don't leak shingles into
    each other (a claim grounded only in source A must NOT pass against source B).
    """
    a = Draft(event_body="華山文創園區本週末舉辦美食市集。", quotes=[])
    # Same claim, a totally unrelated source: must now be ungrounded.
    r_b = verify_grounding(a, "完全不相關的內容描述，講的是天氣與交通狀況。")
    assert r_b.needs_human_review


def test_injected_strategy_uses_protocol_seam():
    """A non-baseline injected strategy is called via is_grounded(claim, source)
    — the precompute fast-path must apply ONLY to SubstringOverlapStrategy."""
    calls: list[tuple[str, str]] = []

    class AlwaysGrounded:
        def is_grounded(self, claim: str, source: str) -> bool:
            calls.append((claim, source))
            return True

    d = Draft(
        event_body="這是一句明顯不在來源裡的敘述內容夠長以通過長度門檻。",
        quotes=[],
    )
    r = verify_grounding(d, SOURCE, AlwaysGrounded())
    assert r.passed  # the injected strategy grounded everything
    assert calls  # ...and it was actually consulted (seam not bypassed)
    assert all(src == grounding.sanitize_source(SOURCE) for _, src in calls)


def test_baseline_fastpath_matches_seam_across_edge_cases():
    """The fast path (precomputed grams) and the plain seam must agree for the
    baseline across edge cases — this guards the early-exit ordering (verbatim
    short-circuit, empty claim, sub-n-gram claim) against a future refactor that
    reorders them. Covers empty/whitespace/punct claims and empty/empty-source.
    """
    strat = SubstringOverlapStrategy()
    sources = [SOURCE, "", "   ", "！？。"]
    claims = [
        "華山文創園區本週末舉辦美食市集",  # verbatim substring
        "完全不相關的內容描述天氣",  # below-threshold overlap
        "",  # empty claim
        "   ",  # whitespace-only
        "！？",  # punctuation-only -> normalizes to empty
        "市",  # shorter than the n-gram size after normalize
    ]
    for source in sources:
        cleaned = grounding.sanitize_source(source)
        grams = grounding._char_shingles(cleaned)
        for claim in claims:
            assert strat._is_grounded(claim, cleaned, grams) == strat.is_grounded(claim, cleaned), (
                claim,
                source,
            )
