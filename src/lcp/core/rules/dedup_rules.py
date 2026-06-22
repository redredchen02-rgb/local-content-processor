"""Pure dedup judgement — no I/O, no exceptions for "duplicate content".

Mirrors :mod:`lcp.core.rules.asset_rules`: facts in (the candidate's title/body
plus an in-memory index handed over by the adapter), a structured
:class:`DedupResult` out. The adapter (``processor/dedup_checker``) is the one
that loads the local job index / published-URL index off disk; this module only
*scores*.

Cheap-first cascade (plan best-practice 級聯; calibration deferred):
  1. **normalized title hash** exact match — lowercase / strip punctuation /
     drop stopwords + site suffix, then sha1. O(1) lookup, catches reposts.
  2. **source-text MinHash + LSH** candidate retrieval, then an **exact Jaccard
     re-verify** on each candidate (LSH gives recall, exact Jaccard gives the
     precision so we don't over-confidently call a duplicate).

Thresholds are PARAMS (Unit 1 spike / 自家語料校準). A hard cosine/Jaccard cutoff
is dangerous, so the band between "clearly unique" and "clearly duplicate" maps
to ``uncertain`` -> needs_human_review(reason=dedup), never a silent verdict.

**fail-loud honesty** (plan R36): ``unique`` only ever means "not seen by THIS
tool against the index it was given". If the published/site index is absent or
possibly incomplete, the adapter passes ``reliability=LOW`` and we MUST NOT emit
a confident ``unique`` — we downgrade to ``uncertain`` with a warning. We NEVER
auto-reject; ``duplicate`` is advisory and maps to the DUPLICATE state for the
caller, but the human is the real gate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

from datasketch import MinHash, MinHashLSH

# --- Tunable defaults (calibration pending — plan Deferred) -------------------

DEFAULT_NUM_PERM = 128  # MinHash permutations; accuracy/speed trade-off.
DEFAULT_SHINGLE_K = 3  # word-shingle size for the MinHash signature.
# LSH retrieves *candidates* at >= this approximate Jaccard. Recall-leaning.
DEFAULT_LSH_THRESHOLD = 0.5
# Exact-Jaccard re-verify bands (PARAMS — calibrate on our corpus):
DEFAULT_DUPLICATE_JACCARD = 0.8  # >= => duplicate
DEFAULT_UNCERTAIN_JACCARD = 0.5  # [this, duplicate) => uncertain (-> human)
# below DEFAULT_UNCERTAIN_JACCARD => not a body match

# Stopwords + site suffixes stripped during title normalization. Starting list.
_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "與", "的", "了"}
)
_SITE_SUFFIXES: tuple[str, ...] = (
    "| ettoday",
    "- 自由時報",
    "｜聯合新聞網",
    "- 中央社",
    "| 蘋果新聞網",
    "- youtube",
)
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


# --- Result types ------------------------------------------------------------


class DedupStatus(str, Enum):
    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    UNCERTAIN = "uncertain"


class DedupReliability(str, Enum):
    """How much to trust this verdict (plan R36 fail-loud)."""

    HIGH = "high"  # a real, believed-complete index was searched
    LOW = "low"  # no/partial index — verdict is decorative for `unique`


@dataclass(frozen=True)
class MatchedItem:
    """A candidate the cascade matched against (PII-free: job_id + scores)."""

    job_id: str
    stage: str  # "title_hash" | "minhash_lsh"
    jaccard: float | None = None  # exact Jaccard (None for title-hash stage)


@dataclass(frozen=True)
class DedupResult:
    """Structured dedup outcome (analogue of asset_rules.Decision).

    * ``status`` — unique / duplicate / uncertain.
    * ``matched_items`` — what we matched against (for the review packet).
    * ``queries`` — the keyword query groups generated (R21), for transparency.
    * ``decision_reason`` — short PII-free explanation.
    * ``reliability`` — HIGH/LOW; LOW forbids a confident ``unique``.
    * ``warnings`` — fail-loud notes (e.g. "no site index").
    """

    status: DedupStatus
    matched_items: list[MatchedItem] = field(default_factory=list)
    queries: list["DedupQuery"] = field(default_factory=list)
    decision_reason: str = ""
    reliability: DedupReliability = DedupReliability.HIGH
    warnings: list[str] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return self.status == DedupStatus.DUPLICATE

    @property
    def is_uncertain(self) -> bool:
        return self.status == DedupStatus.UNCERTAIN

    @property
    def is_unique(self) -> bool:
        return self.status == DedupStatus.UNIQUE


# --- Index value object (the adapter fills this from disk) --------------------


@dataclass(frozen=True)
class IndexEntry:
    """One previously-seen item the candidate is compared against."""

    job_id: str
    title: str = ""
    body: str = ""


@dataclass(frozen=True)
class DedupIndex:
    """In-memory snapshot of the local job / published index.

    ``site_index_available`` is the honesty switch (R36): the adapter sets it
    False when there is no published/site index to compare against (or it may be
    incomplete). The pure layer then refuses a confident ``unique``."""

    entries: tuple[IndexEntry, ...] = ()
    site_index_available: bool = True

    @property
    def is_empty(self) -> bool:
        return len(self.entries) == 0


# --- Title normalization + hashing (cascade stage 1) -------------------------


def normalize_title(title: str) -> str:
    """Lowercase, strip a known site suffix, drop punctuation + stopwords,
    collapse whitespace. Deterministic, pure."""
    t = title.strip().lower()
    for suffix in _SITE_SUFFIXES:
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
    t = _PUNCT_RE.sub(" ", t)
    tokens = [tok for tok in _WS_RE.split(t) if tok and tok not in _TITLE_STOPWORDS]
    return " ".join(tokens)


def title_hash(title: str) -> str:
    """sha1 of the normalized title — the stage-1 exact-match key."""
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()


# --- MinHash helpers (cascade stage 2) ---------------------------------------


def _shingles(text: str, k: int) -> set[str]:
    tokens = [tok for tok in _WS_RE.split(_PUNCT_RE.sub(" ", text.lower())) if tok]
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def build_minhash(
    text: str, *, num_perm: int = DEFAULT_NUM_PERM, k: int = DEFAULT_SHINGLE_K
) -> MinHash:
    m = MinHash(num_perm=num_perm)
    for sh in _shingles(text, k):
        m.update(sh.encode("utf-8"))
    return m


# --- LSH index cache (avoids rebuilding on every dedup call) -------------------
# A module-level cache keyed by (index_fingerprint, lsh_threshold, num_perm).
# The fingerprint is a hash of all entry bodies, so the cache invalidates
# automatically when the index content changes. This eliminates the
# O(|index| × |shingles|) MinHash rebuild on every dedup call in batch mode.

_lsh_cache: dict[tuple[str, float, int], MinHashLSH] = {}
_lsh_sigs_cache: dict[tuple[str, float, int], dict[str, MinHash]] = {}


def _index_fingerprint(index: DedupIndex) -> str:
    """Deterministic fingerprint of index content for cache keying."""
    h = hashlib.sha256()
    for entry in index.entries:
        h.update(entry.job_id.encode("utf-8"))
        h.update(entry.body.encode("utf-8"))
    return h.hexdigest()[:16]


def _get_or_build_lsh(
    index: DedupIndex,
    lsh_threshold: float,
    num_perm: int,
    k: int,
) -> tuple[MinHashLSH, dict[str, MinHash]]:
    """Return (lsh, signatures) from cache or build fresh."""
    fp = _index_fingerprint(index)
    key = (fp, lsh_threshold, num_perm)
    if key in _lsh_cache:
        return _lsh_cache[key], _lsh_sigs_cache[key]

    lsh = MinHashLSH(threshold=lsh_threshold, num_perm=num_perm)
    signatures: dict[str, MinHash] = {}
    for entry in index.entries:
        if not entry.body.strip():
            continue
        sig = build_minhash(entry.body, num_perm=num_perm, k=k)
        signatures[entry.job_id] = sig
        if entry.job_id not in lsh:
            lsh.insert(entry.job_id, sig)

    # Bound the cache to prevent unbounded growth (max 32 recent indices).
    if len(_lsh_cache) >= 32:
        oldest = next(iter(_lsh_cache))
        del _lsh_cache[oldest]
        del _lsh_sigs_cache[oldest]
    _lsh_cache[key] = lsh
    _lsh_sigs_cache[key] = signatures
    return lsh, signatures


def exact_jaccard(a: str, b: str, *, k: int = DEFAULT_SHINGLE_K) -> float:
    """Exact set-Jaccard over word-shingles — the precision re-verify step."""
    sa, sb = _shingles(a, k), _shingles(b, k)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# --- Query groups (R21) ------------------------------------------------------


@dataclass(frozen=True)
class DedupQuery:
    """One keyword query group the operator/tool would run against the index
    (plan R21: at least two groups). Tokens are PII-aware but stored as the
    operator's chosen search terms, not raw scraped fields."""

    group: str  # "person_account_event" | "place_platform_school_event"
    terms: list[str] = field(default_factory=list)


def build_queries(
    *,
    person_or_account: str | None = None,
    event: str | None = None,
    place_or_platform_or_school: str | None = None,
    core_event: str | None = None,
) -> list[DedupQuery]:
    """Build the two R21 query groups:
      1. person/account + event
      2. place/platform/school + core-event
    Empty terms are dropped; each group is returned even if partial (so the
    caller can see what *could* be searched)."""
    g1 = [t for t in (person_or_account, event) if t]
    g2 = [t for t in (place_or_platform_or_school, core_event) if t]
    return [
        DedupQuery(group="person_account_event", terms=g1),
        DedupQuery(group="place_platform_school_event", terms=g2),
    ]


# --- The cascade (pure scoring -> DedupResult) -------------------------------


def assess_dedup(
    *,
    title: str,
    body: str,
    index: DedupIndex,
    queries: list[DedupQuery] | None = None,
    duplicate_jaccard: float = DEFAULT_DUPLICATE_JACCARD,
    uncertain_jaccard: float = DEFAULT_UNCERTAIN_JACCARD,
    lsh_threshold: float = DEFAULT_LSH_THRESHOLD,
    num_perm: int = DEFAULT_NUM_PERM,
    k: int = DEFAULT_SHINGLE_K,
) -> DedupResult:
    """Run the cheap-first cascade and fold into a :class:`DedupResult`.

    Precedence:
      1. stage-1 normalized-title-hash exact match -> duplicate.
      2. stage-2 MinHash/LSH candidate -> exact-Jaccard re-verify:
           >= duplicate_jaccard -> duplicate
           [uncertain_jaccard, duplicate_jaccard) -> uncertain
      3. else -> unique, BUT downgraded to uncertain if reliability is LOW
         (no/partial site index — honesty over decoration, R36).

    Pure: no disk; `index` is handed in. NEVER auto-reject; `duplicate` is
    advisory (caller maps to the DUPLICATE state)."""
    queries = queries or []
    reliability = DedupReliability.HIGH if index.site_index_available else DedupReliability.LOW
    warnings: list[str] = []
    if not index.site_index_available:
        warnings.append(
            "site/published index unavailable or incomplete: 'unique' cannot be "
            "asserted confidently (reliability=low)"
        )

    # --- Stage 1: normalized title hash exact match ---
    # An EMPTY normalized title (emoji-only / punctuation-only / all-stopword)
    # carries NO duplication signal — skip the title-hash match so two unrelated
    # such articles don't collide on the hash of "" -> false terminal DUPLICATE
    # (U4). Fall through to the body comparison instead. Also guard the entry side
    # so a real-titled candidate never matches an empty-titled index entry.
    cand_norm = normalize_title(title)
    if cand_norm:
        cand_th = title_hash(title)
        for entry in index.entries:
            if normalize_title(entry.title) and title_hash(entry.title) == cand_th:
                return DedupResult(
                    status=DedupStatus.DUPLICATE,
                    matched_items=[MatchedItem(entry.job_id, "title_hash")],
                    queries=queries,
                    decision_reason="normalized title hash exact match",
                    reliability=reliability,
                    warnings=warnings,
                )

    # --- Stage 2: MinHash/LSH candidate retrieval + exact Jaccard re-verify ---
    if body.strip() and not index.is_empty:
        lsh, signatures = _get_or_build_lsh(index, lsh_threshold, num_perm, k)
        cand_sig = build_minhash(body, num_perm=num_perm, k=k)
        candidate_ids = lsh.query(cand_sig) if signatures else []

        best: MatchedItem | None = None
        uncertain_hits: list[MatchedItem] = []
        body_by_id = {e.job_id: e.body for e in index.entries}
        for cid in candidate_ids:
            j = exact_jaccard(body, body_by_id.get(cid, ""), k=k)
            item = MatchedItem(cid, "minhash_lsh", jaccard=round(j, 4))
            if j >= duplicate_jaccard:
                if best is None or (item.jaccard or 0) > (best.jaccard or 0):
                    best = item
            elif j >= uncertain_jaccard:
                uncertain_hits.append(item)

        if best is not None:
            return DedupResult(
                status=DedupStatus.DUPLICATE,
                matched_items=[best],
                queries=queries,
                decision_reason=(f"body MinHash+exact-Jaccard match >= {duplicate_jaccard}"),
                reliability=reliability,
                warnings=warnings,
            )
        if uncertain_hits:
            return DedupResult(
                status=DedupStatus.UNCERTAIN,
                matched_items=uncertain_hits,
                queries=queries,
                decision_reason=(
                    f"body similarity in [{uncertain_jaccard}, {duplicate_jaccard}) "
                    "— undecided, needs human review"
                ),
                reliability=reliability,
                warnings=warnings,
            )

    # --- No match. Honesty gate: LOW reliability cannot assert unique. ---
    if reliability == DedupReliability.LOW:
        return DedupResult(
            status=DedupStatus.UNCERTAIN,
            matched_items=[],
            queries=queries,
            decision_reason=(
                "no match found, but no trustworthy site index to confirm uniqueness (fail-loud)"
            ),
            reliability=DedupReliability.LOW,
            warnings=warnings,
        )

    return DedupResult(
        status=DedupStatus.UNIQUE,
        matched_items=[],
        queries=queries,
        decision_reason="not seen by this tool against the searched index",
        reliability=DedupReliability.HIGH,
        warnings=warnings,
    )
