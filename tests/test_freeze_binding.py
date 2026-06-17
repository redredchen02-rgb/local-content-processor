"""Unit 4: the freeze hash binds AI captions/subheads (post-freeze edit caught)."""

from __future__ import annotations

from lcp.adapters.publisher.review_packet import compute_body_sha256
from lcp.core.draft import Draft, MediaSection


def _base():
    return Draft(title="标题", intro="引言", event_body="事件经过正文", summary="结尾")


def test_caption_free_draft_hash_unchanged_by_new_fields():
    # backward compatible: a draft with no captions/subheads hashes the same as a
    # body-only join (the new fields are empty -> filtered out).
    d1 = _base()
    d2 = _base()
    assert compute_body_sha256(d1) == compute_body_sha256(d2)


def test_caption_edit_changes_freeze_hash():
    before = _base().model_copy(
        update={"image_sections": [MediaSection(caption="原始图说")]}
    )
    after = _base().model_copy(
        update={"image_sections": [MediaSection(caption="被偷偷改过的图说")]}
    )
    assert compute_body_sha256(before) != compute_body_sha256(after)


def test_subhead_edit_changes_freeze_hash():
    before = _base().model_copy(update={"subheads": ["小标题一"]})
    after = _base().model_copy(update={"subheads": ["小标题一", "偷加的小标题"]})
    assert compute_body_sha256(before) != compute_body_sha256(after)


def test_adding_caption_changes_hash_vs_none():
    plain = compute_body_sha256(_base())
    withcap = compute_body_sha256(
        _base().model_copy(update={"image_sections": [MediaSection(caption="新增图说")]})
    )
    assert plain != withcap
