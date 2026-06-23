"""Unit tests for sop_steps.steps_for() — pure state-to-step mapping."""

from lcp.core.rules.sop_steps import steps_for
from lcp.core.state import JobState, ReviewReason

_ALL_KW = [
    "人物:周冬雨",
    "地點:上海",
    "平台:微博",
    "事件:緋聞",
    "內容類型:八卦",
]


def _step(steps, n):
    return steps[n - 1]  # 1-indexed


# --- U4 spec test scenarios ---------------------------------------------------


def test_review_pending_all_kw_steps_01_09_done_step_10_pending():
    """REVIEW_PENDING + category + all 5 dimensions → steps 01–09 done, 10 pending."""
    steps = steps_for(
        JobState.REVIEW_PENDING,
        draft_fields={"category": "娛樂", "keywords": _ALL_KW},
    )
    for i in range(1, 10):
        assert _step(steps, i).done, f"step {i:02d} should be done"
    s10 = _step(steps, 10)
    assert not s10.done
    assert s10.current


def test_blocked_risk_step_02_blocked_rest_not_yet():
    """BLOCKED + review_reason=RISK → step 02 blocked, steps 03–10 not done."""
    steps = steps_for(JobState.BLOCKED, review_reason=ReviewReason.RISK)
    assert _step(steps, 1).done
    assert _step(steps, 2).blocked
    assert _step(steps, 2).blocked_label == "內容風險攔截"
    for i in range(3, 11):
        s = _step(steps, i)
        assert not s.done, f"step {i:02d} should not be done when BLOCKED"
        assert not s.blocked, f"step {i:02d} should not itself be blocked"


def test_duplicate_step_03_blocked():
    """DUPLICATE → step 03 blocked."""
    steps = steps_for(JobState.DUPLICATE)
    assert _step(steps, 3).blocked
    assert _step(steps, 3).blocked_label == "站內重複"
    assert not _step(steps, 2).blocked


def test_processed_no_category_step_04_not_done():
    """PROCESSED + category=None → step 04 not done (not blocked, not not_yet)."""
    steps = steps_for(
        JobState.PROCESSED,
        draft_fields={"category": None, "keywords": _ALL_KW},
    )
    s04 = _step(steps, 4)
    assert not s04.done
    assert not s04.blocked
    assert not s04.not_yet


def test_classification_hold_step_04_blocked():
    """NEEDS_HUMAN_REVIEW + CLASSIFICATION → step 04 blocked."""
    steps = steps_for(
        JobState.NEEDS_HUMAN_REVIEW,
        review_reason=ReviewReason.CLASSIFICATION,
        draft_fields={"category": None, "keywords": []},
    )
    s04 = _step(steps, 4)
    assert s04.blocked
    assert s04.blocked_label == "需人工確認分類"


def test_processed_partial_keywords_step_08_not_done():
    """PROCESSED + only KEYWORD_PERSON entries → step 08 not done (missing 4 dims)."""
    steps = steps_for(
        JobState.PROCESSED,
        draft_fields={"category": "娛樂", "keywords": ["人物:周冬雨"]},
    )
    s08 = _step(steps, 8)
    assert not s08.done
    assert not s08.blocked


def test_new_state_only_step_01_pending():
    """NEW state → step 01 is current, all others not done."""
    steps = steps_for(JobState.NEW)
    assert not _step(steps, 1).done
    assert _step(steps, 1).current
    for i in range(2, 11):
        assert not _step(steps, i).done


def test_draft_unavailable_steps_04_08_not_yet():
    """Pre-PROCESSED (draft_fields=None) → steps 04–08 are not_yet, not failed."""
    steps = steps_for(JobState.CRAWLED, draft_fields=None)
    for i in (4, 5, 6, 7, 8):
        s = _step(steps, i)
        assert not s.done
        assert not s.blocked


def test_published_recorded_all_done():
    """PUBLISHED_RECORDED → all 10 steps done."""
    steps = steps_for(
        JobState.PUBLISHED_RECORDED,
        draft_fields={"category": "娛樂", "keywords": _ALL_KW},
    )
    for i in range(1, 11):
        assert _step(steps, i).done, f"step {i:02d} should be done at PUBLISHED_RECORDED"


def test_grounding_hold_step_07_blocked():
    """NEEDS_HUMAN_REVIEW + GROUNDING → step 07 blocked."""
    steps = steps_for(
        JobState.NEEDS_HUMAN_REVIEW,
        review_reason=ReviewReason.GROUNDING,
        draft_fields={"category": "娛樂", "keywords": []},
    )
    s07 = _step(steps, 7)
    assert s07.blocked
    assert s07.blocked_label == "文案需修訂"


def test_step_count_always_ten():
    """steps_for() always returns exactly 10 steps."""
    for state in JobState:
        assert len(steps_for(state)) == 10
