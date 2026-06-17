"""Unit 4: captions/subheads are grounded like body claims."""

from __future__ import annotations

from lcp.core.draft import Draft, MediaSection
from lcp.core.rules.grounding import verify_grounding

SOURCE = (
    "当事人昨天在记者会上公开道歉。现场有上百名记者出席。"
    "事件起因是一笔有争议的合约纠纷。"
)


def test_grounded_caption_passes():
    draft = Draft(
        title="t", intro="i", event_body="当事人昨天在记者会上公开道歉",
        image_sections=[MediaSection(caption="现场有上百名记者出席")],
    )
    assert verify_grounding(draft, SOURCE).passed


def test_ungrounded_caption_routes_to_human():
    draft = Draft(
        title="t", intro="i", event_body="当事人昨天在记者会上公开道歉",
        image_sections=[MediaSection(caption="当事人其实是外星人秘密统治地球")],
    )
    res = verify_grounding(draft, SOURCE)
    assert res.needs_human_review
    assert any(u.kind == "claim" for u in res.ungrounded_claims)


def test_ungrounded_subhead_routes_to_human():
    draft = Draft(
        title="t", intro="i", event_body="当事人昨天在记者会上公开道歉",
        subheads=["完全无关的虚构小标题内容一二三"],
    )
    assert verify_grounding(draft, SOURCE).needs_human_review
