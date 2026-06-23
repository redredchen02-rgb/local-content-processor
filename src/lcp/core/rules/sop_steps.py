"""SOP 10-step workflow progress derivation (pure, no I/O).

Given a job's current state and draft fields, returns the completion status
of each editorial SOP step so the GUI can render a progress panel without a
new server endpoint.

Step-to-state mapping (from the plan's High-Level Technical Design table):

  01 接收素材    done: state != NEW
  02 素材检查    done: state in AFTER_CRAWL; blocked: BLOCKED (redline)
  03 站内查重    done: same as 02 AND state != DUPLICATE; blocked: DUPLICATE
  04 确认方向    done: draft.category truthy; blocked: reason == CLASSIFICATION
  05 处理图片    done: state in AFTER_PROCESSED
  06 制作封面    done: same as 05
  07 撰写文案    done: same as 05; blocked: reason in {GROUNDING, LINT}
  08 填写关键词  done: same as 07 AND all 5 keyword dimensions present
  09 草稿箱      done: state in {REVIEW_PENDING, APPROVED, PUBLISHED_RECORDED}
  10 发群/发布   done: PUBLISHED_RECORDED; pending otherwise
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lcp.core.state import JobState, ReviewReason

_AFTER_CRAWL: frozenset[JobState] = frozenset(
    {
        JobState.CRAWLED,
        JobState.CRAWLED_WARN,
        JobState.PROCESSING,
        JobState.PROCESSED,
        JobState.NEEDS_HUMAN_REVIEW,
        JobState.NEEDS_REVISION,
        JobState.PROCESS_FAILED,
        JobState.REVIEW_PENDING,
        JobState.APPROVED,
        JobState.PUBLISHED_RECORDED,
    }
)

_AFTER_PROCESSED: frozenset[JobState] = frozenset(
    {
        JobState.PROCESSED,
        JobState.NEEDS_HUMAN_REVIEW,
        JobState.NEEDS_REVISION,
        JobState.REVIEW_PENDING,
        JobState.APPROVED,
        JobState.PUBLISHED_RECORDED,
    }
)

_DRAFT_BLOCKING_REASONS: frozenset[ReviewReason] = frozenset({ReviewReason.GROUNDING})

_KW_DIMENSIONS: tuple[str, ...] = ("人物:", "地點:", "平台:", "事件:", "內容類型:")


@dataclass(frozen=True)
class StepStatus:
    label: str
    done: bool
    current: bool
    blocked: bool
    blocked_label: str = ""
    not_yet: bool = False


def steps_for(
    state: JobState,
    review_reason: Optional[ReviewReason] = None,
    draft_fields: Optional[dict[str, object]] = None,
) -> list[StepStatus]:
    """Return the 10-step SOP progress list derived from job state + draft.

    `draft_fields` should contain `category` and `keywords` keys from
    `sanitize_draft()` output. When None (pre-PROCESSED), steps 04–08 are
    shown as not-yet-reached rather than incomplete.
    """
    d = draft_fields or {}
    draft_available = draft_fields is not None

    # --- helpers ---------------------------------------------------------------

    def _done_or_not_yet(done: bool) -> tuple[bool, bool]:
        """Return (done, not_yet): if draft unavailable, step is not-yet, not failed."""
        if not draft_available:
            return False, True
        return done, False

    def _all_kw_dimensions(keywords: list[object]) -> bool:
        return all(any(str(kw).startswith(dim) for kw in keywords) for dim in _KW_DIMENSIONS)

    # --- step 01 接收素材 -------------------------------------------------------
    s01_done = state != JobState.NEW
    s01_current = state == JobState.NEW

    # --- step 02 素材检查 -------------------------------------------------------
    s02_blocked = state == JobState.BLOCKED
    s02_done = state in _AFTER_CRAWL and not s02_blocked
    s02_current = state in {JobState.CRAWLED, JobState.CRAWLED_WARN} and not s02_blocked

    # --- step 03 站内查重 -------------------------------------------------------
    s03_blocked = state == JobState.DUPLICATE
    s03_done = (state in _AFTER_CRAWL) and not s02_blocked and not s03_blocked
    s03_current = s02_done and not s03_done and not s03_blocked

    # --- step 04 确认方向 -------------------------------------------------------
    s04_blocked = review_reason == ReviewReason.CLASSIFICATION
    category = d.get("category") or None
    s04_done_raw = bool(category)
    s04_done, s04_not_yet = _done_or_not_yet(s04_done_raw)
    if s04_blocked:
        s04_done = False
        s04_not_yet = False
    s04_current = s03_done and not s04_done and not s04_blocked and not s04_not_yet

    # --- step 05 处理图片 -------------------------------------------------------
    s05_done = state in _AFTER_PROCESSED
    s05_current = s04_done and not s05_done and not s04_blocked

    # --- step 06 制作封面 -------------------------------------------------------
    s06_done = s05_done
    # --- step 07 撰写文案 -------------------------------------------------------
    s07_blocked = review_reason in _DRAFT_BLOCKING_REASONS
    s07_done = s05_done and not s07_blocked
    s07_current = s05_done and not s07_done and not s07_blocked

    # --- step 08 填写关键词 -----------------------------------------------------
    raw_kw = d.get("keywords")
    keywords: list[object] = list(raw_kw) if isinstance(raw_kw, (list, tuple)) else []
    s08_done_raw = s07_done and _all_kw_dimensions(keywords)
    s08_done, s08_not_yet = _done_or_not_yet(s08_done_raw)
    if not draft_available:
        s08_not_yet = not s07_done  # before PROCESSED → not yet
    s08_current = s07_done and not s08_done and not s08_not_yet

    # --- step 09 草稿箱 ---------------------------------------------------------
    s09_done = state in {
        JobState.REVIEW_PENDING,
        JobState.APPROVED,
        JobState.PUBLISHED_RECORDED,
    }
    s09_current = s08_done and not s09_done

    # --- step 10 发群/发布 -------------------------------------------------------
    s10_done = state == JobState.PUBLISHED_RECORDED
    s10_current = s09_done and not s10_done

    return [
        StepStatus("01 接收素材", s01_done, s01_current, False),
        StepStatus("02 素材检查", s02_done, s02_current, s02_blocked, "內容風險攔截"),
        StepStatus("03 站内查重", s03_done, s03_current, s03_blocked, "站內重複"),
        StepStatus(
            "04 确认方向", s04_done, s04_current, s04_blocked, "需人工確認分類", s04_not_yet
        ),
        StepStatus("05 处理图片", s05_done, s05_current, False),
        StepStatus("06 制作封面", s06_done, False, False),
        StepStatus("07 撰写文案", s07_done, s07_current, s07_blocked, "文案需修訂"),
        StepStatus("08 填写关键词", s08_done, s08_current, False, "", s08_not_yet),
        StepStatus("09 草稿箱", s09_done, s09_current, False),
        StepStatus("10 发群/发布", s10_done, s10_current, False),
    ]
