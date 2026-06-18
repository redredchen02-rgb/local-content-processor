"""Pure draft lint — structure / quality judgement of a :class:`Draft`.

Mirrors :mod:`lcp.core.rules.asset_rules` / ``risk_rules`` / ``dedup_rules``:
facts in (a Draft + a tiny config view + optional source paragraphs), a
structured :class:`LintResult` out. NOTHING here touches disk, network, or a
clock, and — critically (R41 / lethal-trifecta redline 3) — it MUST NOT parse,
resolve, or fetch any URL. A draft/source string that happens to contain a URL
is treated as inert text; we only ever do local string comparison.

What it checks (plan Unit 7b, R17):
  * title present + length within [title_min, title_max]
  * intro present
  * required canonical sections present (一分鐘快速看懂 / 事件經過 / 圖片展示 /
    FAQ / 結尾)
  * video section present IFF videos exist (flag a mismatch)
  * tags count within [tag_min, tag_max] and objective (no hype/clickbait words)
  * keywords consistent with the body (each keyword appears in the draft text)
  * category is one of the configured categories
  * no duplicate paragraphs inside the draft
  * no verbatim copy of source paragraphs that is "too much" (copied-too-much)

Severity model (analogue of asset_rules.Decision / risk_rules.RiskResult):
  * ``errors``   -> status ``needs_revision`` (the draft is fixable; re-run in
    place). Structural/quality problems live here.
  * ``blocked``  -> status ``blocked`` for the hardest lint failures (excessive
    verbatim copying of the source — a copyright/quality red flag the assembler
    should never produce). This maps to NEEDS_REVISION at the gate too (lint is
    advisory; the human is the gate) but is surfaced distinctly for metrics.
  * ``warnings`` never change the status on their own.

Like the other rule modules the thresholds/word-lists are *starting baselines*
to be calibrated; they are parameters so callers can extend them from config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..draft import Draft

# --- Canonical required sections (plan R17) ----------------------------------

# The human-facing section labels (for messages) keyed by the Draft attribute we
# verify is non-empty. image_sections and video_sections are handled separately
# (present IFF the bundle has images / videos respectively — D9).
REQUIRED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("intro", "引言"),
    ("quick_facts", "一分鐘快速看懂"),
    ("event_body", "事件經過"),
    ("faq", "FAQ"),
    ("summary", "結尾"),
)

# Hype / clickbait markers a tag must not contain (objective-tags rule, R17).
# Lowercased substring match; starting baseline, extend via config.
DEFAULT_HYPE_WORDS: tuple[str, ...] = (
    "爆款", "必看", "炸裂", "震驚", "驚爆", "狂", "神級", "逆天", "秒殺", "瘋傳",
    "史上最", "破表", "嚇傻", "崩潰", "clickbait", "shocking", "insane",
)

# A source paragraph copied verbatim into the draft body that is at least this
# many characters counts as "copied-too-much" material (calibration pending).
DEFAULT_MIN_COPY_CHARS = 40
# If the fraction of (long) source paragraphs reproduced verbatim reaches this,
# the draft is BLOCKED rather than merely needs_revision (excessive copying).
DEFAULT_BLOCK_COPY_RATIO = 0.5
# A paragraph shorter than this is ignored for duplicate/copy detection (noise).
_MIN_PARAGRAPH_CHARS = 12


class LintStatus(str, Enum):
    PASS = "pass"
    NEEDS_REVISION = "needs_revision"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class LintResult:
    """Structured lint outcome (analogue of asset_rules.Decision).

    * ``status`` — pass / needs_revision / blocked.
    * ``errors`` — problems that force needs_revision (PII-free strings).
    * ``warnings`` — advisory notes that never change the status alone.
    * ``score`` — a 0..1 quality score (1.0 = no errors/warnings); purely a
      hint for the review packet, never a gate.
    """

    status: LintStatus
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score: float = 1.0

    @property
    def passed(self) -> bool:
        return self.status == LintStatus.PASS

    @property
    def needs_revision(self) -> bool:
        return self.status == LintStatus.NEEDS_REVISION

    @property
    def blocked(self) -> bool:
        return self.status == LintStatus.BLOCKED


@dataclass(frozen=True)
class LintConfig:
    """The slice of ContentConfig the linter needs, as a pure value object so
    the rule module never imports the adapters/config loader. The adapter builds
    this from :class:`lcp.core.config.ContentConfig`."""

    title_min_chars: int = 25
    title_max_chars: int = 35
    tag_min_count: int = 3
    tag_max_count: int = 5
    categories: tuple[str, ...] = ()
    hype_words: tuple[str, ...] = DEFAULT_HYPE_WORDS
    min_copy_chars: int = DEFAULT_MIN_COPY_CHARS
    block_copy_ratio: float = DEFAULT_BLOCK_COPY_RATIO


_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Whitespace-collapsed, stripped — for paragraph comparison. Pure."""
    return _WS_RE.sub(" ", text).strip()


def _paragraphs(text: str) -> list[str]:
    """Split body text into non-trivial paragraphs (blank-line or newline
    separated). Pure string op — no URL parsing, no I/O."""
    if not text:
        return []
    parts = re.split(r"\n\s*\n|\n", text)
    return [p for p in (_norm(x) for x in parts) if len(p) >= _MIN_PARAGRAPH_CHARS]


def _draft_text(draft: Draft) -> str:
    """All human-readable draft text concatenated, for keyword-consistency.
    Captions and quotes included; no URL is parsed — plain concatenation."""
    chunks: list[str] = [draft.title, draft.intro, draft.event_body, draft.summary]
    chunks.extend(draft.quick_facts)
    chunks.extend(s.caption for s in draft.image_sections)
    chunks.extend(s.caption for s in draft.video_sections)
    for item in draft.faq:
        chunks.append(item.question)
        chunks.append(item.answer)
    chunks.extend(q.text for q in draft.quotes)
    return "\n".join(c for c in chunks if c)


def _section_present(draft: Draft, attr: str) -> bool:
    value = getattr(draft, attr)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)  # list sections: non-empty


def lint_draft(
    draft: Draft,
    config: LintConfig,
    *,
    source_paragraphs: list[str] | None = None,
    has_videos: bool = False,
    has_images: bool = False,
) -> LintResult:
    """Lint a Draft against structural/quality rules. Pure: no I/O, no URL parse.

    `source_paragraphs` (optional) is the cleaned source split into paragraphs —
    the caller cleans it with the SAME ``sanitize_source`` used for grounding;
    we only do local substring/equality comparison to detect copied-too-much.
    `has_videos`/`has_images` say whether the job actually has video/image assets
    (so we can flag a missing image/video section IFF the bundle has that media —
    D9). Returns a structured :class:`LintResult`."""
    errors: list[str] = []
    warnings: list[str] = []
    block = False

    # --- title -------------------------------------------------------------
    title = (draft.title or "").strip()
    if not title:
        errors.append("title missing")
    else:
        n = len(title)
        if n < config.title_min_chars:
            errors.append(
                f"title too short: {n} < {config.title_min_chars} chars"
            )
        elif n > config.title_max_chars:
            errors.append(
                f"title too long: {n} > {config.title_max_chars} chars"
            )

    # --- required sections (intro is one of them) --------------------------
    for attr, label in REQUIRED_SECTIONS:
        if not _section_present(draft, attr):
            errors.append(f"missing required section: {label}")

    # --- image section required IFF the bundle has images (D9) -------------
    # Asymmetric on purpose: captions may legitimately exist without bundle
    # images (the copywriter generates them), so a present-without-images
    # section is NOT flagged — only a missing one when images exist.
    if has_images and not draft.image_sections:
        errors.append("圖片展示 section missing while images exist")

    # --- video section present IFF videos exist ----------------------------
    has_video_section = bool(draft.video_sections)
    if has_videos and not has_video_section:
        errors.append("影片介紹 section missing while videos exist")
    elif has_video_section and not has_videos:
        warnings.append("影片介紹 section present but no video assets")

    # --- tags: count in range + objective (no hype) ------------------------
    tag_n = len(draft.tags)
    if tag_n < config.tag_min_count:
        errors.append(
            f"too few tags: {tag_n} < {config.tag_min_count}"
        )
    elif tag_n > config.tag_max_count:
        errors.append(
            f"too many tags: {tag_n} > {config.tag_max_count}"
        )
    hype_hits = _hype_tags(draft.tags, config.hype_words)
    if hype_hits:
        errors.append(f"hype/clickbait tag(s) not objective: {hype_hits}")

    # --- keywords consistent with body -------------------------------------
    body_text = _draft_text(draft).lower()
    orphan_keywords = [
        kw for kw in draft.keywords if kw.strip() and kw.strip().lower() not in body_text
    ]
    if orphan_keywords:
        warnings.append(
            f"keyword(s) not found in draft body: {orphan_keywords}"
        )

    # --- category in config ------------------------------------------------
    if config.categories:
        if not draft.category:
            errors.append("category missing")
        elif draft.category not in config.categories:
            errors.append(f"category not in config: {draft.category!r}")

    # --- duplicate paragraphs inside the draft -----------------------------
    body_paras = _paragraphs(draft.event_body)
    dup = _duplicate_paragraphs(body_paras)
    if dup:
        warnings.append(f"{dup} duplicate paragraph(s) in body")

    # --- copied-too-much: verbatim source paragraphs ----------------------
    if source_paragraphs:
        copied, ratio = _copied_too_much(
            body_paras, source_paragraphs, config.min_copy_chars
        )
        if copied:
            if ratio >= config.block_copy_ratio:
                block = True
                errors.append(
                    f"excessive verbatim copy of source: {copied} long "
                    f"paragraph(s) reproduced ({ratio:.0%} of source)"
                )
            else:
                errors.append(
                    f"verbatim copy of source: {copied} long paragraph(s) "
                    f"reproduced — rewrite extractively"
                )

    score = _score(errors, warnings)
    if block:
        return LintResult(LintStatus.BLOCKED, errors, warnings, score)
    if errors:
        return LintResult(LintStatus.NEEDS_REVISION, errors, warnings, score)
    return LintResult(LintStatus.PASS, errors, warnings, score)


# --- helpers (pure) ----------------------------------------------------------


def _hype_tags(tags: list[str], hype_words: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    lowered = [w.lower() for w in hype_words]
    for tag in tags:
        t = tag.lower()
        if any(w in t for w in lowered):
            hits.append(tag)
    return hits


def _duplicate_paragraphs(paragraphs: list[str]) -> int:
    """Count paragraphs that appear more than once (each extra occurrence)."""
    seen: set[str] = set()
    dups = 0
    for p in paragraphs:
        if p in seen:
            dups += 1
        else:
            seen.add(p)
    return dups


def _copied_too_much(
    body_paragraphs: list[str],
    source_paragraphs: list[str],
    min_copy_chars: int,
) -> tuple[int, float]:
    """Count how many *long* source paragraphs were reproduced verbatim in the
    body, and that count as a fraction of the long source paragraphs. Pure
    string equality — no URL is touched."""
    long_source = [p for p in source_paragraphs if len(p) >= min_copy_chars]
    if not long_source:
        return 0, 0.0
    source_set = set(long_source)
    body_set = set(body_paragraphs)
    copied = sum(1 for p in source_set if p in body_set)
    return copied, copied / len(long_source)


def _score(errors: list[str], warnings: list[str]) -> float:
    """A coarse 0..1 quality hint: each error costs more than each warning.
    Never used as a gate — purely informational for the review packet."""
    penalty = 0.2 * len(errors) + 0.05 * len(warnings)
    return max(0.0, round(1.0 - penalty, 3))
