"""Pure grounding-verification tests — zero I/O (plan Unit 7b: "純").

Covers: verbatim quotes must be source substrings; narrative claims checked via
the pluggable strategy (baseline now, NLI seam later); fail -> needs_human_review
(reason grounding); and the SECURITY invariant that grounding never resolves a
URL (negative assertion).
"""

from __future__ import annotations

import socket

import pytest

from lcp.core.draft import Draft, FaqItem, SourceQuote
from lcp.core.rules import grounding
from lcp.core.rules.grounding import (
    GroundingStatus,
    GroundingStrategy,
    SubstringOverlapStrategy,
    verify_grounding,
)

SOURCE = (
    "華山文創園區本週末舉辦美食市集。\n"
    "現場有上百個攤位提供各式小吃與飲料。\n"
    "主辦單位預估將吸引大量人潮前往參觀。"
)


# --- Happy path: quotes verbatim + claims supported --------------------------


def test_grounded_draft_passes():
    d = Draft(
        title="美食市集",
        event_body="華山文創園區本週末舉辦美食市集。現場有上百個攤位提供各式小吃與飲料。",
        faq=[FaqItem(question="有什麼？", answer="現場有上百個攤位提供各式小吃與飲料")],
        quotes=[
            SourceQuote(text="華山文創園區本週末舉辦美食市集。"),
            SourceQuote(text="現場有上百個攤位提供各式小吃與飲料。"),
        ],
    )
    r = verify_grounding(d, SOURCE)
    assert r.status == GroundingStatus.PASS
    assert r.passed
    assert r.ungrounded_claims == []


# --- Unit 1 (B0 fix): generated quick_facts + summary must be grounded too ----


def test_ungrounded_quick_fact_routes_to_human():
    d = Draft(
        event_body="華山文創園區本週末舉辦美食市集。",
        quick_facts=["主辦單位涉嫌收受廠商鉅額回扣遭檢方約談偵訊"],  # not in source
        quotes=[],
    )
    r = verify_grounding(d, SOURCE)
    assert r.needs_human_review
    assert any(u.kind == "claim" for u in r.ungrounded_claims)


def test_ungrounded_summary_routes_to_human():
    d = Draft(
        event_body="華山文創園區本週末舉辦美食市集。",
        summary="據傳市長與廠商之間存在不可告人的金錢往來與利益輸送關係",  # absent
        quotes=[],
    )
    r = verify_grounding(d, SOURCE)
    assert r.needs_human_review


def test_grounded_quick_fact_and_summary_pass():
    d = Draft(
        event_body="華山文創園區本週末舉辦美食市集。",
        quick_facts=["現場有上百個攤位提供各式小吃與飲料"],  # substring of SOURCE
        summary="主辦單位預估將吸引大量人潮前往參觀",  # substring of SOURCE
        quotes=[],
    )
    r = verify_grounding(d, SOURCE)
    assert r.passed


# --- Fail: quote not in source ----------------------------------------------


def test_quote_not_in_source_routes_to_human():
    d = Draft(
        event_body="華山文創園區本週末舉辦美食市集。",
        quotes=[SourceQuote(text="主辦單位收受廠商賄賂三百萬元")],  # not in source
    )
    r = verify_grounding(d, SOURCE)
    assert r.status == GroundingStatus.NEEDS_HUMAN_REVIEW
    assert r.needs_human_review
    assert any(u.kind == "quote" for u in r.ungrounded_claims)
    assert "quote" in r.reason


# --- Fail: claim/accusation absent from source ("出現來源沒有的指控") ----------


def test_unsupported_accusation_claim_routes_to_human():
    d = Draft(
        # an accusation that does not appear in the source at all
        event_body="市長疑似收受巨額賄賂並掩蓋醜聞遭到檢方約談偵訊。",
        quotes=[],
    )
    r = verify_grounding(d, SOURCE)
    assert r.status == GroundingStatus.NEEDS_HUMAN_REVIEW
    assert any(u.kind == "claim" for u in r.ungrounded_claims)


def test_faq_answer_unsupported_routes_to_human():
    d = Draft(
        event_body="華山文創園區本週末舉辦美食市集。",
        faq=[FaqItem(question="花費？", answer="據傳活動花費超過五千萬元並涉及貪污舞弊。")],
        quotes=[],
    )
    r = verify_grounding(d, SOURCE)
    assert r.needs_human_review
    assert any(u.kind == "claim" for u in r.ungrounded_claims)


# --- Cleaning parity with the assembler --------------------------------------


def test_quote_with_hidden_codepoint_in_source_still_grounds():
    """The source carries a zero-width char; sanitize_source strips it on both
    the assembly side and here, so a clean-substring quote still grounds."""
    dirty_source = "華山​文創園區舉辦美食市集活動。"  # zero-width space
    d = Draft(
        event_body="華山文創園區舉辦美食市集活動。",
        quotes=[SourceQuote(text="華山文創園區舉辦美食市集活動。")],  # the cleaned form
    )
    r = verify_grounding(d, dirty_source)
    assert r.passed


# --- Pluggable strategy: baseline now, NLI seam later ------------------------


def test_default_strategy_is_substring_overlap():
    assert isinstance(SubstringOverlapStrategy(), GroundingStrategy)


def test_custom_strategy_is_used():
    """A drop-in strategy (the U1/NLI seam) is honoured unchanged — here a stub
    that entails everything makes even an unsupported claim pass."""

    class AlwaysEntails:
        def is_grounded(self, claim: str, source: str) -> bool:
            return True

    d = Draft(event_body="完全與來源無關的虛構指控內容文字。", quotes=[])
    r = verify_grounding(d, SOURCE, strategy=AlwaysEntails())
    assert r.passed  # the strategy vouched for the claim


def test_strict_strategy_can_fail_a_borderline_claim():
    class AlwaysRejects:
        def is_grounded(self, claim: str, source: str) -> bool:
            return False

    d = Draft(event_body="華山文創園區本週末舉辦美食市集。", quotes=[])
    r = verify_grounding(d, SOURCE, strategy=AlwaysRejects())
    assert r.needs_human_review


def test_overlap_threshold_param_respected():
    # A claim that shares most tokens with the source but isn't a substring.
    strat = SubstringOverlapStrategy(overlap_threshold=0.5)
    assert strat.is_grounded("美食市集 攤位 小吃", SOURCE) is True
    strict = SubstringOverlapStrategy(overlap_threshold=0.99)
    assert strict.is_grounded("美食 完全不相干 詞彙 隨機", SOURCE) is False


# --- SECURITY: grounding never resolves a URL --------------------------------


def test_grounding_makes_no_network_request(monkeypatch):
    """Negative assertion (R41 / redline 3): a draft + source full of URLs must
    cause ZERO network activity. We trap every socket entry; grounding only does
    local string comparison so nothing should fire."""

    def _boom(*a, **k):
        raise AssertionError("grounding must not open a socket / resolve a URL")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "gethostbyname", _boom)

    url_source = "http://169.254.169.254/latest http://attacker.test/x 內文文字。"
    d = Draft(
        event_body="http://attacker.test/payload 應被當作純文字比對。",
        quotes=[SourceQuote(text="http://attacker.test/x")],
    )
    r = verify_grounding(d, url_source)  # must not raise
    assert r.status in (
        GroundingStatus.PASS,
        GroundingStatus.NEEDS_HUMAN_REVIEW,
    )


def test_grounding_module_imports_no_url_libraries():
    import sys

    mod = sys.modules[grounding.__name__]
    src = open(mod.__file__, encoding="utf-8").read()
    for forbidden in ("import urllib", "import requests", "import socket", "import httpx"):
        assert forbidden not in src, f"{forbidden!r} must not appear in grounding"
