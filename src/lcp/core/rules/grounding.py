"""Pure grounding verification — is the draft actually supported by the source?

Mirrors the other rule modules (facts in -> structured result out, no I/O). Two
checks (plan Unit 7b, R23):

  (a) **verbatim quotes** — every ``Draft.quotes[*].text`` MUST be a verbatim
      substring of the *cleaned* source. We clean the source with the SAME
      function the assembler used (:func:`lcp.adapters.llm.assembler.sanitize_source`)
      so a quote that was a real substring of the cleaned source at assembly time
      stays one here. Importing that pure function is allowed — it has no I/O.

  (b) **narrative claims** (event_body / faq) — checked against the source via a
      PLUGGABLE :class:`GroundingStrategy` (Protocol). The default
      :class:`SubstringOverlapStrategy` is the zero-dependency BASELINE: a claim
      is grounded if it is a verbatim substring of the cleaned source OR shares
      enough token overlap with it. The seam lets Unit 1 swap in a stronger
      claim-level NLI strategy (MiniCheck/SummaC) WITHOUT changing this module's
      result shape or the adapter that consumes it.

REDLINE 3 (lethal-trifecta / R41): this module performs ONLY local string
comparison. It MUST NOT parse, resolve, or fetch any URL. A draft or source that
contains a URL is treated as inert text — there is no urllib/socket/requests
import here and none is reachable. ``extractive != faithful``: "has a quote" is
not proof of faithfulness, which is exactly why we verify claims, not just
quote-presence.

Outcome: a draft that is fully grounded -> ``pass``; any ungrounded quote/claim
-> ``needs_human_review`` (reason=grounding). The adapter maps that to
NEEDS_HUMAN_REVIEW(reason=grounding); after a human clears it, lint re-runs
(plan 架構審查 2d)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from ..draft import Draft
from ..text_sanitize import sanitize_source  # core-local: no upward import

# Token overlap >= this fraction of the claim's tokens counts the claim as
# grounded in the baseline strategy (calibration pending — Unit 1 spike).
DEFAULT_OVERLAP_THRESHOLD = 0.6
# Claims shorter than this (after normalization) are skipped — too short to
# verify meaningfully and dominated by stopwords.
_MIN_CLAIM_CHARS = 8

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
# Character n-gram size for the overlap fallback. Character shingles work for
# both space-delimited text AND CJK (which has no word boundaries), so the
# baseline doesn't depend on a tokenizer/segmenter.
_OVERLAP_NGRAM = 2


class GroundingStatus(str, Enum):
    PASS = "pass"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


@dataclass(frozen=True)
class UngroundedClaim:
    """One claim/quote that could not be grounded (PII-free for audit: kind +
    a short marker; the full text stays in the review packet, not the audit)."""

    kind: str  # "quote" | "claim"
    text: str  # the offending text (consumed by the review packet, not audit)
    detail: str = ""


@dataclass(frozen=True)
class GroundingResult:
    """Structured grounding outcome (analogue of risk_rules.RiskResult).

    * ``status`` — pass / needs_human_review.
    * ``ungrounded_claims`` — every quote/claim that failed grounding.
    * ``reason`` — short PII-free explanation (empty when passed).
    """

    status: GroundingStatus
    ungrounded_claims: list[UngroundedClaim] = field(default_factory=list)
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.status == GroundingStatus.PASS

    @property
    def needs_human_review(self) -> bool:
        return self.status == GroundingStatus.NEEDS_HUMAN_REVIEW


# --- Pluggable strategy seam (U1 swaps strength in here) ----------------------


@runtime_checkable
class GroundingStrategy(Protocol):
    """The strength-pluggable seam for narrative-claim grounding.

    A strategy decides whether a single ``claim`` is supported by the already
    cleaned ``source`` — ONLY by local comparison (no URL parsing/fetch). U1's
    chosen strength implements THIS protocol; :func:`verify_grounding` and the
    adapter never change. The baseline is substring/overlap; an NLI strategy
    (MiniCheck/SummaC) would return entailment instead, behind the same call."""

    def is_grounded(self, claim: str, source: str) -> bool: ...


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation/whitespace — for character-shingle overlap.
    Pure local string op (no URL parsing)."""
    return _WS_RE.sub("", _PUNCT_RE.sub("", text.lower()))


def _char_shingles(text: str, n: int = _OVERLAP_NGRAM) -> frozenset[str]:
    """Character n-grams of the normalized text. Works for CJK (no word
    boundaries) AND space-delimited text without a tokenizer/segmenter.

    NOT memoized with a module-global cache: the key would be the raw source /
    claim TEXT (PII-bearing), so an LRU would retain it in process memory ACROSS
    jobs — unacceptable for a compliance-first pipeline. verify_grounding instead
    shingles the cleaned source ONCE per call and reuses it across claims (see
    below), so the former O(claims x |source|) cost is avoided with NO cross-job
    retention. Returns a frozenset (callers only ever read it)."""
    s = _normalize(text)
    if len(s) < n:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i : i + n] for i in range(len(s) - n + 1))


@dataclass(frozen=True)
class SubstringOverlapStrategy:
    """Zero-dependency BASELINE grounding strategy.

    A claim is grounded if it is a verbatim substring of the cleaned source, or
    if at least ``overlap_threshold`` of its character n-grams appear in the
    source's. Character shingles are used (not word tokens) so the baseline works
    for CJK as well as space-delimited text without a segmenter. Deliberately
    lenient (the gate fails *closed* to a human on a miss, so a false
    "ungrounded" costs a human review, not a wrong publish). Pure: local string
    ops only — NO URL is parsed/resolved/fetched."""

    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD

    def is_grounded(self, claim: str, source: str) -> bool:
        """Protocol seam: shingles the source itself. verify_grounding uses the
        precomputed-source fast path (``_is_grounded``) to shingle the source
        ONCE and reuse it across every claim instead of re-shingling per claim."""
        return self._is_grounded(claim, source, _char_shingles(source))

    def _is_grounded(self, claim: str, source: str, source_grams: frozenset[str]) -> bool:
        """Overlap check against an ALREADY-shingled source (shared across the
        claims of one verify_grounding call — see _char_shingles' cache note)."""
        c = claim.strip()
        if not c:
            return True
        if c in source:
            return True
        claim_grams = _char_shingles(c)
        if not claim_grams:
            return True
        present = sum(1 for g in claim_grams if g in source_grams)
        return present / len(claim_grams) >= self.overlap_threshold


# NLI seam placeholder: Unit 1 may provide e.g.
#
#     @dataclass(frozen=True)
#     class NliStrategy:
#         model: "MiniCheckModel"
#         def is_grounded(self, claim, source) -> bool:
#             return self.model.entails(premise=source, hypothesis=claim)
#
# It would be passed to verify_grounding(...) unchanged. Still local-only —
# the model reasons over the strings, it does not resolve any URL.


# --- The verifier (pure orchestration of the two checks) ---------------------


def _split_claims(draft: Draft) -> list[str]:
    """Narrative claims to verify: intro sentences + event_body sentences +
    faq answers + the net-new AI structural pieces (image/video captions +
    subheads, Unit 4).  Captions/subheads are generated content, so they must
    be grounded too — an ungrounded caption routes the job to human review,
    never silently passes.  intro is an independent LLM-generated field; its
    factual claims must be verified as substrings of the source.
    Pure splitting on sentence boundaries / newlines — no URL parsing."""
    def _keep(items: list[str]) -> list[str]:
        return [s for s in items if len(s) >= _MIN_CLAIM_CHARS]

    claims: list[str] = []
    claims.extend(_keep(_sentences(draft.intro)))
    claims.extend(_keep(_sentences(draft.event_body)))
    claims.extend(_keep([item.answer.strip() for item in draft.faq]))
    claims.extend(_keep([s.caption.strip() for s in (*draft.image_sections, *draft.video_sections)]))
    claims.extend(_keep([sub.strip() for sub in draft.subheads]))
    # Unit 1 (B0 fix): quick_facts items + summary sentences are copywriter-
    # generated narrative content, so they are grounded too — an ungrounded
    # quick fact / summary routes to a human, never silently passes.
    claims.extend(_keep([qf.strip() for qf in draft.quick_facts]))
    claims.extend(_keep(_sentences(draft.summary)))
    return claims


def _sentences(text: str) -> list[str]:
    if not text:
        return []
    # Split on CJK + ASCII sentence terminators and newlines.
    parts = re.split(r"[。！？!?\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def verify_grounding(
    draft: Draft,
    source_text: str,
    strategy: GroundingStrategy | None = None,
) -> GroundingResult:
    """Verify a Draft against its source. Pure: local string comparison only —
    NO URL is parsed, resolved, or fetched (R41 / redline 3).

    Steps:
      1. clean the source with the assembler's ``sanitize_source`` (identical
         cleaning to assembly time, so verbatim quotes still match).
      2. every ``draft.quotes[*].text`` must be a substring of the cleaned
         source — else it's an ungrounded quote.
      3. each narrative claim (event_body sentences + faq answers) is checked by
         ``strategy`` (baseline substring/overlap; U1 may inject NLI).

    Any ungrounded quote/claim -> ``needs_human_review`` (reason=grounding)."""
    cleaned = sanitize_source(source_text or "")
    strat = strategy if strategy is not None else SubstringOverlapStrategy()
    ungrounded: list[UngroundedClaim] = []

    # (a) verbatim quotes MUST be substrings of the cleaned source.
    for quote in draft.quotes:
        qt = quote.text
        if qt and qt not in cleaned:
            ungrounded.append(
                UngroundedClaim(
                    kind="quote",
                    text=qt,
                    detail="quote is not a verbatim substring of the source",
                )
            )

    # (b) narrative claims checked via the pluggable strategy. The baseline
    # shingles the cleaned source ONCE here and reuses it across every claim (no
    # module-global cache — the PII-bearing source must not persist past this
    # call). An injected (e.g. NLI) strategy uses the plain Protocol seam.
    # Exact-type (not isinstance): the precompute fast-path is valid ONLY for the
    # baseline itself. A subclass that overrides is_grounded must reach its
    # override via the Protocol seam below — never be silently routed through the
    # parent's _is_grounded. (No subclasses exist today; this is defensive.)
    baseline = strat if type(strat) is SubstringOverlapStrategy else None
    source_grams = _char_shingles(cleaned) if baseline is not None else frozenset()
    for claim in _split_claims(draft):
        grounded = (
            baseline._is_grounded(claim, cleaned, source_grams)
            if baseline is not None
            else strat.is_grounded(claim, cleaned)
        )
        if not grounded:
            ungrounded.append(
                UngroundedClaim(
                    kind="claim",
                    text=claim,
                    detail="claim not supported by the source",
                )
            )

    if ungrounded:
        n_q = sum(1 for u in ungrounded if u.kind == "quote")
        n_c = sum(1 for u in ungrounded if u.kind == "claim")
        return GroundingResult(
            status=GroundingStatus.NEEDS_HUMAN_REVIEW,
            ungrounded_claims=ungrounded,
            reason=f"ungrounded: {n_q} quote(s), {n_c} claim(s)",
        )
    return GroundingResult(status=GroundingStatus.PASS)
