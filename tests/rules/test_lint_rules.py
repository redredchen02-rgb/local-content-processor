"""Pure draft-lint tests — zero file/subprocess I/O (plan Unit 7b: "純").

Also pins the security invariant: lint never parses/resolves/fetches a URL —
even a draft full of URLs only ever gets local string comparison.
"""

from __future__ import annotations

import socket

from lcp.core.draft import Draft, FaqItem, MediaSection
from lcp.core.rules import lint_rules
from lcp.core.rules.lint_rules import (
    LintConfig,
    LintStatus,
    lint_draft,
)

CATEGORIES = ("社會", "娛樂", "美食")

CFG = LintConfig(
    title_min_chars=10,
    title_max_chars=35,
    tag_min_count=3,
    tag_max_count=5,
    categories=CATEGORIES,
    # Keep existing tests passing by setting loose field-level constraints.
    # The new strict-defaults are exercised by the dedicated Unit 1 tests below.
    intro_min_chars=1,
    intro_max_chars=9999,
    event_body_min_chars=1,
    event_body_max_chars=9999,
    summary_warn_chars=9998,
    summary_error_chars=9999,
    faq_min_count=1,
    faq_max_count=99,
    quick_facts_min_count=1,
    quick_facts_max_count=99,
)


def _good_draft(**overrides) -> Draft:
    """A well-formed draft satisfying every lint rule."""
    base = dict(
        title="台北週末美食市集盛大登場好熱鬧",  # within [10,35]
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


# --- Happy path --------------------------------------------------------------


def test_well_formed_draft_passes():
    r = lint_draft(_good_draft(), CFG)
    assert r.status == LintStatus.PASS
    assert r.passed
    assert r.errors == []
    assert r.score == 1.0


# --- title length ------------------------------------------------------------


def test_title_too_long_needs_revision():
    r = lint_draft(_good_draft(title="標" * 40), CFG)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("title too long" in e for e in r.errors)


# --- D9: image_sections is conditional (required iff the bundle has images) ---


def test_image_sections_not_required_for_text_only_article():
    # A text-only draft (no bundle images) passes without an image section.
    r = lint_draft(_good_draft(image_sections=[]), CFG, has_images=False)
    assert r.passed, r.errors


def test_image_sections_required_when_bundle_has_images():
    r = lint_draft(_good_draft(image_sections=[]), CFG, has_images=True)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("圖片展示" in e for e in r.errors)


def test_well_formed_draft_with_images_passes_when_has_images():
    r = lint_draft(_good_draft(), CFG, has_images=True)
    assert r.passed, r.errors


def test_title_too_short_needs_revision():
    r = lint_draft(_good_draft(title="短"), CFG)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("title too short" in e for e in r.errors)


def test_title_missing_needs_revision():
    r = lint_draft(_good_draft(title=""), CFG)
    assert any("title missing" in e for e in r.errors)


# --- required sections -------------------------------------------------------


def test_missing_intro_flagged():
    r = lint_draft(_good_draft(intro=""), CFG)
    assert any("引言" in e for e in r.errors)


def test_missing_event_body_flagged():
    r = lint_draft(_good_draft(event_body=""), CFG)
    assert any("事件經過" in e for e in r.errors)


def test_missing_faq_and_summary_flagged():
    r = lint_draft(_good_draft(faq=[], summary=""), CFG)
    assert any("FAQ" in e for e in r.errors)
    assert any("結尾" in e for e in r.errors)


# --- video section present IFF videos ----------------------------------------


def test_video_section_missing_while_videos_exist_flagged():
    r = lint_draft(_good_draft(), CFG, has_videos=True)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("影片介紹 section missing" in e for e in r.errors)


def test_video_section_present_with_videos_passes():
    d = _good_draft(video_sections=[MediaSection(asset_ref="v/a.mp4", caption="影片")])
    r = lint_draft(d, CFG, has_videos=True)
    assert r.passed


def test_video_section_present_without_videos_is_warning_only():
    d = _good_draft(video_sections=[MediaSection(asset_ref="v/a.mp4", caption="影片")])
    r = lint_draft(d, CFG, has_videos=False)
    assert r.passed  # warning, not an error
    assert any("no video assets" in w for w in r.warnings)


# --- tags --------------------------------------------------------------------


def test_too_few_tags_needs_revision():
    r = lint_draft(_good_draft(tags=["美食"]), CFG)
    assert any("too few tags" in e for e in r.errors)


def test_too_many_tags_needs_revision():
    r = lint_draft(_good_draft(tags=["a", "b", "c", "d", "e", "f"]), CFG)
    assert any("too many tags" in e for e in r.errors)


def test_hype_word_tag_is_not_objective():
    r = lint_draft(_good_draft(tags=["美食", "爆款", "必看"]), CFG)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("hype" in e for e in r.errors)


# --- category ----------------------------------------------------------------


def test_category_not_in_config_needs_revision():
    r = lint_draft(_good_draft(category="政治"), CFG)
    assert any("category not in config" in e for e in r.errors)


def test_category_missing_needs_revision():
    r = lint_draft(_good_draft(category=None), CFG)
    assert any("category missing" in e for e in r.errors)


# --- keywords ----------------------------------------------------------------


def test_orphan_keyword_is_warning_not_error():
    r = lint_draft(_good_draft(keywords=["完全沒出現的詞XYZ"]), CFG)
    # keyword inconsistency is a warning; if nothing else fails, still passes
    assert r.passed
    assert any("keyword" in w for w in r.warnings)


# --- duplicate paragraphs ----------------------------------------------------


def test_duplicate_paragraphs_warned():
    body = "完全相同的一段內容文字在這裡。\n\n完全相同的一段內容文字在這裡。"
    r = lint_draft(_good_draft(event_body=body), CFG)
    assert any("duplicate paragraph" in w for w in r.warnings)


# --- copied-too-much (verbatim source) ---------------------------------------


def test_verbatim_copy_of_source_paragraph_needs_revision():
    # ONE of THREE long (>=40 char) source paragraphs reproduced -> ratio 1/3
    # < block ratio -> needs_revision (not blocked).
    copied = "這是一段相當長的來源原文段落內容它的字數遠遠超過四十個字元的門檻並且被原封不動地照搬進草稿正文裡面真是太誇張了。"
    other1 = "另一段同樣很長的來源原文段落內容它的字數也明顯超過四十個字元的門檻不過這一段並沒有被照抄進草稿當中所以無妨。"
    other2 = "第三段也是相當長的來源原文段落內容它的字數一樣超過四十個字元的門檻同樣沒有被原封不動地照搬進草稿正文當中。"
    assert min(len(copied), len(other1), len(other2)) >= 40
    d = _good_draft(event_body=copied)
    r = lint_draft(d, CFG, source_paragraphs=[copied, other1, other2])
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("verbatim copy" in e for e in r.errors)


def test_excessive_verbatim_copy_blocks():
    # both long (>=40 char) source paragraphs reproduced -> ratio 1.0 >= block
    p1 = "第一段非常長的來源原文段落內容它的字數明顯超過四十個字元的門檻並且被整段一字不差地照抄進草稿正文當中了。"
    p2 = "第二段同樣很長的來源原文段落內容它的字數也超過四十個字元的門檻而且一字不差地被照搬進了草稿的正文裡面。"
    assert min(len(p1), len(p2)) >= 40
    d = _good_draft(event_body=f"{p1}\n\n{p2}")
    r = lint_draft(d, CFG, source_paragraphs=[p1, p2])
    assert r.status == LintStatus.BLOCKED
    assert r.blocked
    assert any("excessive verbatim copy" in e for e in r.errors)


def test_extractive_rewrite_not_flagged_as_copy():
    src_para = "這是一段相當長的來源原文段落，超過四十個字元的門檻，原始用語與草稿不同。"
    d = _good_draft(event_body="記者改寫後的不同說法，並未逐字照搬來源段落內容。")
    r = lint_draft(d, CFG, source_paragraphs=[src_para])
    assert r.passed  # no verbatim paragraph reproduced


# --- score -------------------------------------------------------------------


def test_score_drops_with_errors():
    r = lint_draft(_good_draft(title="短", tags=["x"]), CFG)
    assert r.score < 1.0


# --- SECURITY: lint never touches a URL --------------------------------------


def test_lint_makes_no_network_request(monkeypatch):
    """Negative assertion (R41 / redline 3): a draft+source full of URLs must
    produce ZERO network activity — lint only does local string comparison.
    We trip every socket entry point; if lint resolves/fetches a URL the test
    fails loudly."""

    def _boom(*a, **k):
        raise AssertionError("lint must not open a socket / resolve a URL")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "gethostbyname", _boom)

    malicious = (
        "http://169.254.169.254/latest/meta-data\n\n"
        "https://evil.example.com/x?a=1 normal body text here for content.\n\n"
        "ftp://10.0.0.1/secret file://etc/passwd"
    )
    d = _good_draft(
        title="含有網址的標題http://attacker.test應視為純文字",
        event_body=malicious,
        keywords=["http://attacker.test"],
    )
    # Must not raise — proves no socket/URL resolution happened.
    r = lint_draft(d, CFG, source_paragraphs=[malicious])
    assert r.status in (LintStatus.PASS, LintStatus.NEEDS_REVISION, LintStatus.BLOCKED)


def test_lint_module_imports_no_url_libraries():
    """Belt-and-braces: the lint rule module must not import urllib/requests/
    socket — there is no code path that could resolve a URL."""
    import sys

    mod = sys.modules[lint_rules.__name__]
    src = open(mod.__file__, encoding="utf-8").read()
    for forbidden in ("import urllib", "import requests", "import socket", "import httpx"):
        assert forbidden not in src, f"{forbidden!r} must not appear in lint_rules"


# --- Unit 1: field-level length / count constraints -------------------------

# LintConfig with strict defaults for Unit 1 tests (mirrors real defaults).
CFG_STRICT = LintConfig(
    title_min_chars=10,
    title_max_chars=35,
    tag_min_count=3,
    tag_max_count=5,
    categories=CATEGORIES,
    # Use LintConfig class defaults for all Unit 1 fields:
    # intro [80,120], event_body [100,200], summary warn=100/error=150,
    # faq [3,5], quick_facts [3,7].
)


def _strict_draft(**overrides) -> Draft:
    """A well-formed draft satisfying all Unit 1 constraints (strict defaults).

    - intro: 88 chars (in [80,120])
    - event_body: 128 chars (in [100,200])
    - summary: 61 chars (≤ summary_warn=100, no warning)
    - faq: 4 items (in [3,5])
    - quick_facts: 5 items (in [3,7])
    """
    base = dict(
        title="台北週末美食市集盛大登場好熱鬧",
        # 88 chars: satisfies intro [80,120]
        intro=(
            "本週末在台北華山文創園區將舉辦規模盛大的年度美食市集活動，"
            "現場聚集超過一百個來自全台各地的特色美食攤位，"
            "主辦單位誠摯歡迎廣大民眾攜家帶眷前來共同參與這場精彩難得的美食文化盛會。"
        ),
        quick_facts=["時間：週六日", "地點：華山", "免費入場", "停車：附近停車場", "交通：捷運"],
        # 128 chars: satisfies event_body [100,200]
        event_body=(
            "台北華山文創園區本週末盛大舉辦年度全台最大規模的美食市集特色活動，"
            "現場超過一百個攤位提供各式在地特色小吃及異國料理美食佳餚供民眾選購品嚐。"
            "主辦單位預計吸引超過萬名以上的民眾前來共襄盛舉同歡，"
            "現場另設有完善的休憩用餐區域提供給民眾休息並悠閒享用各式美食料理。"
        ),
        image_sections=[MediaSection(asset_ref="img/a.jpg", caption="攤位實景")],
        faq=[
            FaqItem(question="需要門票嗎？", answer="不需要，免費入場。"),
            FaqItem(question="開放時間？", answer="早上十點到晚上九點。"),
            FaqItem(question="有停車場嗎？", answer="附近有多處停車場。"),
            FaqItem(question="有哪些美食？", answer="涵蓋台式、日式、韓式等各類料理。"),
        ],
        # 61 chars: satisfies summary ≤ warn=100 (no warning triggered)
        summary="這是一場精彩的週末美食文化盛會活動，現場美食種類豐富，絕對不容錯過，歡迎大家前來共享。",
        tags=["美食", "市集", "華山"],
        keywords=["美食", "市集"],
        category="美食",
    )
    base.update(overrides)
    return Draft(**base)


def test_unit1_happy_path_passes():
    """All Unit 1 constraints satisfied → PASS."""
    r = lint_draft(_strict_draft(), CFG_STRICT)
    assert r.status == LintStatus.PASS, r.errors


def test_unit1_intro_too_short():
    """intro < intro_min_chars (80) → error."""
    short_intro = "短" * 50  # 50 chars < 80
    r = lint_draft(_strict_draft(intro=short_intro), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("intro too short" in e for e in r.errors)


def test_unit1_intro_too_long():
    """intro > intro_max_chars (120) → error."""
    long_intro = "長" * 130  # 130 chars > 120
    r = lint_draft(_strict_draft(intro=long_intro), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("intro too long" in e for e in r.errors)


def test_unit1_event_body_too_short():
    """event_body < event_body_min_chars (100) → error."""
    short_body = "短" * 80  # 80 chars < 100
    r = lint_draft(_strict_draft(event_body=short_body), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("event_body too short" in e for e in r.errors)


def test_unit1_event_body_too_long():
    """event_body > event_body_max_chars (200) → error."""
    long_body = "長" * 210  # 210 chars > 200
    r = lint_draft(_strict_draft(event_body=long_body), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("event_body too long" in e for e in r.errors)


def test_unit1_summary_warning_zone():
    """summary_warn_chars < len(summary) <= summary_error_chars → warning only, not error."""
    # summary_warn=100, summary_error=150 → 110 chars is in the warning zone
    warn_summary = "警" * 110  # 110 chars: 100 < 110 ≤ 150
    r = lint_draft(_strict_draft(summary=warn_summary), CFG_STRICT)
    assert r.status == LintStatus.PASS, f"should pass (warning only), errors={r.errors}"
    assert any("結尾偏長" in w for w in r.warnings)
    assert not any("結尾過長" in e for e in r.errors)


def test_unit1_summary_error_zone():
    """len(summary) > summary_error_chars (150) → error → NEEDS_REVISION."""
    long_summary = "長" * 160  # 160 chars > 150
    r = lint_draft(_strict_draft(summary=long_summary), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("結尾過長" in e for e in r.errors)
    # should NOT also produce the warning when it's already an error
    assert not any("結尾偏長" in w for w in r.warnings)


def test_unit1_too_few_faq():
    """faq items < faq_min_count (3) → error."""
    r = lint_draft(
        _strict_draft(faq=[FaqItem(question="問？", answer="答。"),
                           FaqItem(question="問2？", answer="答2。")]),
        CFG_STRICT,
    )
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("too few FAQ" in e for e in r.errors)


def test_unit1_too_many_faq():
    """faq items > faq_max_count (5) → error."""
    r = lint_draft(
        _strict_draft(faq=[FaqItem(question=f"問{i}？", answer=f"答{i}。") for i in range(6)]),
        CFG_STRICT,
    )
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("too many FAQ" in e for e in r.errors)


def test_unit1_quick_facts_too_few():
    """non-empty quick_facts < quick_facts_min_count (3) → error."""
    r = lint_draft(_strict_draft(quick_facts=["只有一個", "只有兩個"]), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("too few quick_facts" in e for e in r.errors)


def test_unit1_quick_facts_too_many():
    """non-empty quick_facts > quick_facts_max_count (7) → error."""
    r = lint_draft(_strict_draft(quick_facts=[f"項目{i}" for i in range(8)]), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("too many quick_facts" in e for e in r.errors)


def test_unit1_quick_facts_empty_handled_by_required_section_rule():
    """quick_facts=[] is caught by the required-section check, NOT the count rule.
    The count rule only fires when quick_facts is non-empty."""
    r = lint_draft(_strict_draft(quick_facts=[]), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    # Should say "missing required section", not "too few quick_facts"
    assert any("一分鐘快速看懂" in e for e in r.errors)
    assert not any("too few quick_facts" in e for e in r.errors)


def test_unit1_new_hype_word_dingjí_triggers_error():
    """New hype word '顶级' (simplified) in tag → error."""
    r = lint_draft(_strict_draft(tags=["美食", "顶级美味", "市集"]), CFG_STRICT)
    assert r.status == LintStatus.NEEDS_REVISION
    assert any("hype" in e for e in r.errors)


# --- Unit 1 done -------------------------------------------------------------


def test_copied_too_much_denominator_uses_source_set():
    """CORE-1: _copied_too_much must use len(source_set) as denominator, NOT
    len(long_source). When source_paragraphs has duplicates, long_source >
    source_set, so using long_source as denominator yields a ratio too low,
    causing a copied-paragraph to appear below the threshold and slip through."""
    from lcp.core.rules.lint_rules import _copied_too_much

    # 3 copies of the same long paragraph in source, body has that paragraph.
    # unique set size = 1, long_source size = 3.
    # Correct ratio: 1/1 = 1.0 (100% copied)
    # Buggy ratio:   1/3 ≈ 0.33 (would fall below a 50% threshold)
    long_para = "這是一段超過閾值的文字，長度遠超過最小字元限制，應被計入複製統計中。"
    count, ratio = _copied_too_much(
        body_paragraphs=[long_para],
        source_paragraphs=[long_para, long_para, long_para],  # 3 duplicates
        min_copy_chars=10,
    )
    assert count == 1
    assert ratio == 1.0, (
        f"expected ratio 1.0 (1 of 1 unique paragraphs copied), got {ratio:.3f}; "
        "denominator must be len(source_set), not len(long_source)"
    )
