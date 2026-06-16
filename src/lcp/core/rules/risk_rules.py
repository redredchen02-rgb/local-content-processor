"""Pure risk judgement — no I/O, no exceptions for "risky content".

This mirrors :mod:`lcp.core.rules.asset_rules`: pure functions, facts in, a
structured result out. The adapter (``processor/risk_checker``) loads inputs,
calls here, then maps the result onto a :class:`~lcp.core.state.JobState` and
writes audit. NOTHING here touches disk, network, or a clock.

Two tiers of risk (plan Unit 6, R3/R4):
  * **redlines** — hard-stop categories (minor / NCII / hidden-cam / political /
    violence / human-rights / unclear-source / unsupported-claim). A hit means
    ``blocked`` -> caller maps to terminal ``BLOCKED`` (not overridable by
    default).
  * **daily checks** — defamation phrasing, identifiable private PII,
    copyright-source-missing. A hit means ``needs_human_review`` (reason=risk).

**fail-closed** (plan R3/R4, 合規優先): if a detector is *uncertain* or
*unavailable* we return ``needs_human_review`` (reason=risk) — NEVER ``pass``.
Silence is not safety.

**Pluggable detector** (plan: hard-dependency on the Unit 1 spike): the gate
skeleton talks to a :class:`RiskDetector` Protocol. The default
:class:`KeywordRiskDetector` is a conservative rule/keyword baseline; U1 will
later swap in a stronger detector (claim-level NLI) WITHOUT changing this
module's result shape or the adapter that consumes it.

Threshold/keyword note: the keyword lists below are deliberately a *starting
baseline* to be calibrated against our own corpus (Unit 1 spike). They live as
module constants / detector params so callers can extend them from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

# --- Risk categories ---------------------------------------------------------


class RiskCategory(str, Enum):
    """Stored as the enum CODE only (PII-free, like ReviewReason)."""

    # Redlines (hard-stop -> blocked).
    MINOR = "minor"  # minors in a sexual/exploitative context
    NCII = "ncii"  # non-consensual intimate imagery
    HIDDEN_CAM = "hidden_cam"  # covert/up-skirt/voyeur recording
    POLITICAL = "political"  # politically sensitive content
    VIOLENCE = "violence"  # graphic violence / gore
    HUMAN_RIGHTS = "human_rights"  # human-rights-abuse / trafficking material
    UNCLEAR_SOURCE = "unclear_source"  # provenance cannot be established
    UNSUPPORTED_CLAIM = "unsupported_claim"  # serious factual claim w/ no source

    # Daily checks (-> needs_human_review, reason=risk).
    DEFAMATION = "defamation"  # accusatory/defamatory phrasing
    PRIVATE_PII = "private_pii"  # identifiable private individual's PII
    COPYRIGHT_SOURCE_MISSING = "copyright_source_missing"

    # Restricted category, disabled by default (R3) — see is_category_enabled.
    CAMPUS_STUDENT = "campus_student"  # 學生校園


# The hard-stop set. A flag in any of these -> blocked (terminal BLOCKED).
REDLINE_CATEGORIES: frozenset[RiskCategory] = frozenset(
    {
        RiskCategory.MINOR,
        RiskCategory.NCII,
        RiskCategory.HIDDEN_CAM,
        RiskCategory.POLITICAL,
        RiskCategory.VIOLENCE,
        RiskCategory.HUMAN_RIGHTS,
        RiskCategory.UNCLEAR_SOURCE,
        RiskCategory.UNSUPPORTED_CLAIM,
    }
)

# Daily-check set. A flag here (with no redline) -> needs_human_review.
DAILY_CHECK_CATEGORIES: frozenset[RiskCategory] = frozenset(
    {
        RiskCategory.DEFAMATION,
        RiskCategory.PRIVATE_PII,
        RiskCategory.COPYRIGHT_SOURCE_MISSING,
    }
)

# Categories that require an explicit human enable before any auto-processing
# (R3: 校園分類預設停用). They are NOT redlines per se — they are simply OFF
# until a human turns them on for a given run.
DISABLED_BY_DEFAULT_CATEGORIES: frozenset[RiskCategory] = frozenset(
    {RiskCategory.CAMPUS_STUDENT}
)


def is_category_enabled(
    category: RiskCategory,
    *,
    enabled_categories: frozenset[RiskCategory] | set[RiskCategory] | None = None,
) -> bool:
    """Pure predicate: is `category` allowed to be auto-processed?

    Categories in :data:`DISABLED_BY_DEFAULT_CATEGORIES` (e.g. 學生校園) are OFF
    unless explicitly listed in `enabled_categories` (a human opt-in, R3). All
    other categories are enabled by default."""
    if category not in DISABLED_BY_DEFAULT_CATEGORIES:
        return True
    return enabled_categories is not None and category in enabled_categories


# --- Result type -------------------------------------------------------------


class RiskStatus(str, Enum):
    PASS = "pass"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RiskFlag:
    """One detector signal. `confident=False` means the detector is unsure —
    by fail-closed policy an unsure flag escalates to needs_human_review even
    for a daily-check category, and an unsure *redline* still blocks."""

    category: RiskCategory
    reason: str
    confident: bool = True


@dataclass(frozen=True)
class RiskResult:
    """Structured outcome of a risk pass (analogue of asset_rules.Decision).

    * ``status`` — pass / needs_human_review / blocked.
    * ``flags`` — every signal raised (for audit + per-category metrics).
    * ``blocking_reasons`` — human-readable, PII-free strings for the redlines
      that caused a block (empty unless status==blocked).
    * ``recommended_action`` — a short machine-stable hint for the adapter/GUI.
    """

    status: RiskStatus
    flags: list[RiskFlag] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    recommended_action: str = ""

    @property
    def blocked(self) -> bool:
        return self.status == RiskStatus.BLOCKED

    @property
    def needs_human_review(self) -> bool:
        return self.status == RiskStatus.NEEDS_HUMAN_REVIEW

    @property
    def passed(self) -> bool:
        return self.status == RiskStatus.PASS


# --- Pluggable detector interface (U1 swaps strength in here) -----------------


@dataclass(frozen=True)
class RiskInput:
    """Facts handed to a detector. Pure value object — the adapter assembles it
    from the manifest/scraped text; the detector never reads disk itself."""

    title: str = ""
    body: str = ""
    has_source: bool = True  # provenance/citation present?
    contains_serious_claim: bool = False  # caller/upstream signal
    available: bool = True  # False => detector backend unavailable (fail-closed)


@runtime_checkable
class RiskDetector(Protocol):
    """The strength-pluggable seam. A detector inspects a :class:`RiskInput`
    and returns ``(flags, available)``. ``available=False`` signals the backend
    could not run (model down, timeout) so the gate fails closed.

    U1's chosen strength (rule-list vs NLI) implements THIS protocol; the gate
    skeleton (:func:`assess_risk`) and the adapter never change."""

    def detect(self, content: "RiskInput") -> "tuple[list[RiskFlag], bool]":
        ...


# --- Default baseline detector (rule / keyword) ------------------------------

# Conservative keyword baseline. CALIBRATION PENDING (Unit 1 spike): these are
# starting points, intentionally broad on redlines (fail-closed), to be tuned
# against our own annotated corpus. Lowercased substring match.
_REDLINE_KEYWORDS: dict[RiskCategory, tuple[str, ...]] = {
    RiskCategory.MINOR: ("未成年", "兒少", "童", "minor", "underage", "child"),
    RiskCategory.NCII: ("外流", "私密照", "復仇式", "ncii", "revenge porn", "leaked nude"),
    RiskCategory.HIDDEN_CAM: ("偷拍", "針孔", "上空偷", "hidden cam", "upskirt", "voyeur"),
    RiskCategory.POLITICAL: ("政治", "選舉", "政黨", "political", "election"),
    RiskCategory.VIOLENCE: ("血腥", "凌虐", "斬首", "gore", "graphic violence"),
    RiskCategory.HUMAN_RIGHTS: ("人口販運", "強迫勞動", "trafficking", "forced labor"),
}

_DEFAMATION_KEYWORDS: tuple[str, ...] = (
    "詐騙犯", "小三", "渣男", "騙子", "罪犯", "scammer", "fraudster", "cheater",
)

# Naive identifiable-PII signals (full name + contact/address pattern). The
# baseline is intentionally simple; U1/regex hardening can extend it.
_PII_KEYWORDS: tuple[str, ...] = (
    "身分證", "身份證號", "住址", "電話", "手機號", "id number", "home address",
)

_CAMPUS_KEYWORDS: tuple[str, ...] = (
    "國中", "高中", "大學", "校園", "學生", "campus", "high school", "university student",
)


@dataclass(frozen=True)
class KeywordRiskDetector:
    """Default rule/keyword baseline implementing :class:`RiskDetector`.

    Deliberately conservative: it FLAGS, it does not clear. Absence of a keyword
    is *not* proof of safety — the gate's fail-closed logic decides the rest.
    Keyword lists are constructor params so config can extend them."""

    redline_keywords: dict[RiskCategory, tuple[str, ...]] = field(
        default_factory=lambda: dict(_REDLINE_KEYWORDS)
    )
    defamation_keywords: tuple[str, ...] = _DEFAMATION_KEYWORDS
    pii_keywords: tuple[str, ...] = _PII_KEYWORDS
    campus_keywords: tuple[str, ...] = _CAMPUS_KEYWORDS

    def detect(self, content: RiskInput) -> tuple[list[RiskFlag], bool]:
        if not content.available:
            return [], False  # fail-closed: caller escalates to review
        haystack = f"{content.title}\n{content.body}".lower()
        flags: list[RiskFlag] = []

        for category, words in self.redline_keywords.items():
            for w in words:
                if w.lower() in haystack:
                    flags.append(
                        RiskFlag(category, f"redline keyword matched: {category.value}")
                    )
                    break

        for w in self.defamation_keywords:
            if w.lower() in haystack:
                flags.append(
                    RiskFlag(RiskCategory.DEFAMATION, "accusatory/defamatory phrasing")
                )
                break

        for w in self.pii_keywords:
            if w.lower() in haystack:
                flags.append(
                    RiskFlag(RiskCategory.PRIVATE_PII, "identifiable private PII pattern")
                )
                break

        # Provenance / claim checks (these are facts the caller supplies, not
        # keyword guesses).
        if not content.has_source:
            flags.append(
                RiskFlag(RiskCategory.UNCLEAR_SOURCE, "source/provenance not established")
            )
            if content.contains_serious_claim:
                flags.append(
                    RiskFlag(
                        RiskCategory.UNSUPPORTED_CLAIM,
                        "serious claim without a supporting source",
                    )
                )
            flags.append(
                RiskFlag(
                    RiskCategory.COPYRIGHT_SOURCE_MISSING,
                    "copyright source attribution missing",
                )
            )
        return flags, True


# --- The gate (pure orchestration of detector -> RiskResult) -----------------


def assess_risk(
    content: RiskInput,
    detector: RiskDetector | None = None,
    *,
    enabled_categories: frozenset[RiskCategory] | set[RiskCategory] | None = None,
) -> RiskResult:
    """Run `detector` over `content` and fold its flags into a :class:`RiskResult`.

    Fail-closed precedence (most severe wins):
      1. detector unavailable -> needs_human_review (reason=risk).
      2. any REDLINE flag -> blocked (terminal). An *unsure* redline still blocks.
      3. a disabled-by-default category appears in the text but isn't enabled
         -> needs_human_review (a human must opt in, R3).
      4. any daily-check flag, OR any unsure flag -> needs_human_review.
      5. otherwise -> pass.

    Pure: no I/O, deterministic given inputs."""
    det = detector if detector is not None else KeywordRiskDetector()
    flags, available = det.detect(content)

    if not available:
        return RiskResult(
            status=RiskStatus.NEEDS_HUMAN_REVIEW,
            flags=list(flags),
            recommended_action="route_to_human:risk_detector_unavailable",
        )

    redline_flags = [f for f in flags if f.category in REDLINE_CATEGORIES]
    if redline_flags:
        return RiskResult(
            status=RiskStatus.BLOCKED,
            flags=list(flags),
            blocking_reasons=[f"{f.category.value}: {f.reason}" for f in redline_flags],
            recommended_action="block:redline",
        )

    # Restricted-category gate (學生校園 disabled by default). We scan the text
    # with the baseline campus keywords regardless of detector, because this is
    # a policy switch, not a risk signal.
    campus_seen = _mentions_disabled_category(content)
    if campus_seen and not is_category_enabled(
        RiskCategory.CAMPUS_STUDENT, enabled_categories=enabled_categories
    ):
        flags = [*flags, RiskFlag(RiskCategory.CAMPUS_STUDENT, "category disabled by default")]
        return RiskResult(
            status=RiskStatus.NEEDS_HUMAN_REVIEW,
            flags=list(flags),
            recommended_action="route_to_human:category_disabled",
        )

    unsure = any(not f.confident for f in flags)
    daily = [f for f in flags if f.category in DAILY_CHECK_CATEGORIES]
    if daily or unsure:
        return RiskResult(
            status=RiskStatus.NEEDS_HUMAN_REVIEW,
            flags=list(flags),
            recommended_action="route_to_human:daily_check",
        )

    return RiskResult(status=RiskStatus.PASS, flags=list(flags), recommended_action="pass")


def _mentions_disabled_category(content: RiskInput) -> bool:
    """Cheap baseline scan for 學生校園 markers. Adapter/U1 may override by
    passing a category-tagged input later; for the baseline we keyword-scan."""
    haystack = f"{content.title}\n{content.body}".lower()
    return any(w.lower() in haystack for w in _CAMPUS_KEYWORDS)


# --- R5: uncertainty-tone helper (judge-then-apply) --------------------------

# Hedging markers that signal a claim is unverified/rumoured.
_UNCERTAINTY_PREFIX = "網傳"  # could also be 疑似 etc.


def apply_uncertainty_tone(
    claim: str,
    *,
    verified: bool,
    marker: str = _UNCERTAINTY_PREFIX,
) -> str:
    """R5: tag ONLY unverified claims with a hedging marker (網傳/疑似…), and do
    NOT mechanically tag an already-verified neutral fact (judge-then-apply).

    * ``verified=True``  -> return the claim unchanged (no mechanical hedging).
    * ``verified=False`` -> prefix the hedging ``marker`` if not already hedged.

    Pure string transform; the *judgement* of `verified` is the caller's (and is
    exactly what the U1 grounding/NLI strength informs)."""
    text = claim.strip()
    if verified:
        return text
    if text.startswith(marker) or text.startswith("疑似"):
        return text  # already hedged — don't double-tag
    return f"{marker}{text}"
