"""Pure risk judgement tests — zero file/subprocess I/O (plan Unit 6: "純")."""

from __future__ import annotations

from lcp.core.rules import risk_rules
from lcp.core.rules.risk_rules import (
    KeywordRiskDetector,
    RiskCategory,
    RiskFlag,
    RiskInput,
    RiskStatus,
    assess_risk,
    is_category_enabled,
)


# --- Happy path --------------------------------------------------------------


def test_clean_content_passes():
    r = assess_risk(RiskInput(title="本地美食節登場", body="週末市集介紹", has_source=True))
    assert r.status == RiskStatus.PASS
    assert r.passed
    assert r.blocking_reasons == []


# --- Redline -> blocked (terminal, not overridable by default) ---------------


def test_minor_redline_blocks():
    r = assess_risk(RiskInput(title="未成年外流影片", body="...", has_source=True))
    assert r.status == RiskStatus.BLOCKED
    assert r.blocked
    assert r.blocking_reasons  # non-empty
    assert any("minor" in br or "ncii" in br for br in r.blocking_reasons)


def test_ncii_redline_blocks():
    r = assess_risk(RiskInput(title="復仇式私密照", body="x", has_source=True))
    assert r.status == RiskStatus.BLOCKED
    assert any("ncii" in br for br in r.blocking_reasons)


def test_hidden_cam_redline_blocks():
    r = assess_risk(RiskInput(title="偷拍針孔事件", body="x", has_source=True))
    assert r.blocked
    assert any("hidden_cam" in br for br in r.blocking_reasons)


def test_redline_beats_daily_check_precedence():
    # both a redline (violence) and a daily-check (defamation) present:
    # redline wins -> blocked.
    r = assess_risk(RiskInput(title="血腥凌虐 詐騙犯", body="x", has_source=True))
    assert r.status == RiskStatus.BLOCKED


def test_unclear_source_is_redline():
    r = assess_risk(
        RiskInput(title="某事件", body="內容", has_source=False)
    )
    assert r.blocked
    assert any("unclear_source" in br for br in r.blocking_reasons)


def test_unsupported_serious_claim_redline():
    r = assess_risk(
        RiskInput(title="x", body="y", has_source=False, contains_serious_claim=True)
    )
    assert r.blocked
    assert any("unsupported_claim" in br for br in r.blocking_reasons)


# --- U4: footgun redline tokens downgrade to review, never false-BLOCK -------


def test_footgun_political_substring_downgrades_to_review_not_blocked():
    """U4: '政治' is a bare substring of innocent '政治大學' (a university). It must
    NOT drive a terminal BLOCKED; the footgun-only match downgrades to
    NEEDS_HUMAN_REVIEW (never silently PASS)."""
    r = assess_risk(RiskInput(title="政治大學校友會活動", body="校友回娘家", has_source=True))
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW
    assert r.status != RiskStatus.BLOCKED


def test_footgun_minor_substring_downgrades_to_review_not_blocked():
    """'童' is a bare substring of innocent 童話/童年/兒童樂園."""
    for title in ("童話故事改編的新電影上映", "兒童樂園週年慶", "我的童年回憶"):
        r = assess_risk(RiskInput(title=title, body="正文", has_source=True))
        assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW, title
        assert r.status != RiskStatus.BLOCKED, title


def test_real_political_redline_still_blocks():
    """The specific political redline keywords (選舉/政黨) stay HARD redlines."""
    r = assess_risk(RiskInput(title="選舉舞弊疑雲", body="x", has_source=True))
    assert r.status == RiskStatus.BLOCKED
    assert any("political" in br for br in r.blocking_reasons)


def test_real_minor_redline_still_blocks_via_specific_keyword():
    """未成年/兒少 stay HARD redlines (only the bare-substring 童 is downgraded)."""
    r = assess_risk(RiskInput(title="未成年外流影片", body="x", has_source=True))
    assert r.status == RiskStatus.BLOCKED


def test_clean_text_with_no_footgun_still_passes():
    """No footgun, no redline, has source -> PASS (footgun mechanism must not
    over-fire on innocuous content)."""
    r = assess_risk(RiskInput(title="台北華山美食市集週末登場", body="現場有樂團表演", has_source=True))
    assert r.status == RiskStatus.PASS


# --- fail-closed: detector uncertain / unavailable -> needs_human_review -----


class _UnavailableDetector:
    def detect(self, content):
        return [], False  # backend down


def test_unavailable_detector_fails_closed_to_review():
    r = assess_risk(RiskInput(title="anything", body=""), detector=_UnavailableDetector())
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW
    assert not r.passed
    assert "unavailable" in r.recommended_action


class _UnsureRedlineDetector:
    """Returns an *unsure* redline flag — fail-closed still blocks."""

    def detect(self, content):
        return [RiskFlag(RiskCategory.VIOLENCE, "maybe violent", confident=False)], True


def test_unsure_redline_still_blocks():
    r = assess_risk(RiskInput(title="x"), detector=_UnsureRedlineDetector())
    assert r.status == RiskStatus.BLOCKED


class _UnsureDailyDetector:
    def detect(self, content):
        return [RiskFlag(RiskCategory.DEFAMATION, "maybe defamatory", confident=False)], True


def test_unsure_daily_flag_routes_to_human():
    r = assess_risk(RiskInput(title="x"), detector=_UnsureDailyDetector())
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW


# --- daily checks -> needs_human_review --------------------------------------


def test_defamation_phrasing_routes_to_human():
    r = assess_risk(RiskInput(title="他就是詐騙犯", body="x", has_source=True))
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW
    assert any(f.category == RiskCategory.DEFAMATION for f in r.flags)


def test_private_pii_routes_to_human():
    r = assess_risk(RiskInput(title="某人住址與電話曝光", body="x", has_source=True))
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW
    assert any(f.category == RiskCategory.PRIVATE_PII for f in r.flags)


# --- Category disabled-by-default (學生校園, R3) ------------------------------


def test_campus_category_disabled_by_default_routes_to_human():
    r = assess_risk(RiskInput(title="某高中校園活動", body="學生社團", has_source=True))
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW
    assert "category_disabled" in r.recommended_action
    assert any(f.category == RiskCategory.CAMPUS_STUDENT for f in r.flags)


def test_campus_category_passes_when_explicitly_enabled():
    r = assess_risk(
        RiskInput(title="某高中校園活動", body="學生社團", has_source=True),
        enabled_categories=frozenset({RiskCategory.CAMPUS_STUDENT}),
    )
    assert r.status == RiskStatus.PASS


def test_is_category_enabled_predicate():
    assert is_category_enabled(RiskCategory.VIOLENCE)  # normal cats always on
    assert not is_category_enabled(RiskCategory.CAMPUS_STUDENT)
    assert is_category_enabled(
        RiskCategory.CAMPUS_STUDENT,
        enabled_categories={RiskCategory.CAMPUS_STUDENT},
    )


# --- Pluggable detector interface --------------------------------------------


def test_default_detector_is_keyword_detector_and_runtime_checkable():
    assert isinstance(KeywordRiskDetector(), risk_rules.RiskDetector)


def test_custom_detector_strength_swaps_without_changing_gate():
    class StrongDetector:
        def detect(self, content):
            # pretend an NLI model found an unsupported claim with confidence
            if "疫苗致死" in content.body:
                return [RiskFlag(RiskCategory.UNSUPPORTED_CLAIM, "nli: unsupported")], True
            return [], True

    blocked = assess_risk(RiskInput(body="疫苗致死率超高"), detector=StrongDetector())
    assert blocked.status == RiskStatus.BLOCKED  # unsupported_claim is redline
    clean = assess_risk(RiskInput(body="今天天氣晴"), detector=StrongDetector())
    assert clean.status == RiskStatus.PASS


def test_keyword_detector_lists_are_overridable():
    det = KeywordRiskDetector(defamation_keywords=("特定詞",))
    r = assess_risk(RiskInput(body="他是特定詞", has_source=True), detector=det)
    assert r.status == RiskStatus.NEEDS_HUMAN_REVIEW
    # default defamation word no longer triggers
    r2 = assess_risk(RiskInput(body="他是詐騙犯", has_source=True), detector=det)
    assert r2.status == RiskStatus.PASS


# --- R5 uncertainty tone: judge-then-apply -----------------------------------


def test_uncertainty_tone_tags_unverified_claim():
    out = risk_rules.apply_uncertainty_tone("某店家偷工減料", verified=False)
    assert out == "網傳某店家偷工減料"


def test_uncertainty_tone_does_not_tag_verified_neutral_fact():
    out = risk_rules.apply_uncertainty_tone("市府公告週六休館", verified=True)
    assert out == "市府公告週六休館"  # NOT mechanically hedged


def test_uncertainty_tone_does_not_double_tag():
    out = risk_rules.apply_uncertainty_tone("網傳某事", verified=False)
    assert out == "網傳某事"
    out2 = risk_rules.apply_uncertainty_tone("疑似某事", verified=False)
    assert out2 == "疑似某事"
