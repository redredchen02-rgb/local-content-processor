"""Draft: the shared 8-section article schema produced by the content assembler.

Single source of truth for the draft structure that flows: Unit 7a (assemble) ->
Unit 7b (lint + grounding) -> Unit 8 (review packet). Pure core layer — no I/O,
no framework beyond pydantic.

Two invariants the rest of the pipeline relies on:
- Machine output is NEVER trusted: every Draft is born `needs_human_review=True`
  and `constrained_rewrite=True`. Unit 7b lints/grounds it; only a human clears
  it (state machine NEEDS_HUMAN_REVIEW -> PROCESSED).
- Verbatim quotes carried in `SourceQuote.text` MUST be substrings of the
  sanitized source. Unit 7a preserves them as spans; Unit 7b re-verifies them
  against the cleaned source (R23 grounding).

The fixed 8 sections (plan R17): title, intro, 一分鐘快速看懂 (quick_facts),
事件經過 (event_body), 圖片展示 (image_sections), 影片介紹 (video_sections),
FAQ (faq), 結尾 (summary)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DraftStatus(str, Enum):
    """Outcome the assembler attaches to a Draft (distinct from JobState).

    NEEDS_REVISION is the assembler's signal that the LLM output is unusable as
    is (truncated / empty / filtered) and the job must go back through PROCESSING
    — the reason string (e.g. "truncated:length", "empty") records *why*.
    NOT_EXECUTED marks a dry-run stub: no API was called, no tokens spent."""

    DRAFTED = "drafted"
    NEEDS_REVISION = "needs_revision"
    NOT_EXECUTED = "not_executed"


class SourceQuote(BaseModel):
    """A verbatim extract. `text` MUST be a substring of the sanitized source
    (grounding contract). `note` is optional machine annotation, never trusted."""

    text: str
    note: str | None = None


class FaqItem(BaseModel):
    question: str
    answer: str


class MediaSection(BaseModel):
    """An image/video block. `caption` is constrained text; `asset_ref` points at
    a manifest AssetRef path. No URLs are parsed/followed here (output-side
    handling is Unit 7b/9)."""

    asset_ref: str | None = None
    caption: str = ""


class Draft(BaseModel):
    """The fixed 8-section article draft + machine-output safety markers.

    `status`/`review_reason` let the pipeline route the draft without inspecting
    the body. `quotes` holds the verbatim spans the assembler extracted so 7b can
    re-verify grounding without re-running the LLM."""

    # 8 canonical sections (R17).
    title: str = ""
    intro: str = ""
    quick_facts: list[str] = Field(default_factory=list)  # 一分鐘快速看懂
    event_body: str = ""  # 事件經過
    image_sections: list[MediaSection] = Field(default_factory=list)  # 圖片展示
    video_sections: list[MediaSection] = Field(default_factory=list)  # 影片介紹
    faq: list[FaqItem] = Field(default_factory=list)  # FAQ
    summary: str = ""  # 結尾
    # AI-generated structural pieces (Unit 4): grouping subheads + title
    # candidates. Net-new content — born needs_human_review like the rest, bound
    # into the freeze hash (subheads) so a post-freeze edit is detectable.
    subheads: list[str] = Field(default_factory=list)  # 分組小標題
    title_candidates: list[str] = Field(default_factory=list)  # 標題候選

    # Classification / metadata (light here; full lint is Unit 7b, R17).
    tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    category: str | None = None

    # Grounding evidence: verbatim spans this draft drew from the source.
    quotes: list[SourceQuote] = Field(default_factory=list)

    # Machine-output safety markers — NEVER default these to a trusting value.
    status: DraftStatus = DraftStatus.DRAFTED
    needs_human_review: bool = True
    constrained_rewrite: bool = True
    review_reason: str | None = None

    # Provenance for the review packet / audit (Unit 8). Never the api_key.
    model: str | None = None
    finish_reason: str | None = None
    executed: bool = True  # False for dry-run stubs (no API call, no tokens)
