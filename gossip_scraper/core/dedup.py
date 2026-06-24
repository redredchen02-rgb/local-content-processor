"""Cross-platform deduplication for gossip items.

Uses a hybrid similarity metric (character trigram Jaccard + longest common
substring ratio) to detect the same topic appearing on different platforms
with different phrasing. When merged, keeps the entry with highest heat and
records which platforms it appeared on."""

from __future__ import annotations

import re
import unicodedata

from ..models import GossipItem

_SIMILARITY_THRESHOLD = 0.30


def dedup(items: list[GossipItem]) -> list[GossipItem]:
    """Deduplicate gossip items across platforms."""
    if not items:
        return []

    normalized = [_normalize_title(it.title) for it in items]
    merged: list[GossipItem] = []
    used: set[int] = set()

    for i, item in enumerate(items):
        if i in used:
            continue
        group = [item]

        for j in range(i + 1, len(items)):
            if j in used:
                continue
            sim = _hybrid_similarity(normalized[i], normalized[j])
            if sim >= _SIMILARITY_THRESHOLD:
                group.append(items[j])
                used.add(j)

        best = max(group, key=lambda x: x.heat)
        platforms = list({it.platform for it in group})
        merged.append(
            GossipItem(
                platform=best.platform,
                rank=best.rank,
                title=best.title,
                url=best.url,
                heat=best.heat,
                tag=best.tag,
                fetched_at=best.fetched_at,
                hot_change=best.hot_change,
                created_at=best.created_at,
                cross_platform_count=len(platforms),
                merged_from=sorted(platforms),
            )
        )
        used.add(i)

    return merged


def _normalize_title(title: str) -> str:
    """Normalize a title for comparison."""
    t = title.lower()
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[#＃「」【】\[\]《》\"']", "", t)
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[，。、！？：；…—\-–·]", "", t)
    return t


def _hybrid_similarity(a: str, b: str) -> float:
    """Hybrid similarity: max of trigram Jaccard and LCS ratio.

    Trigram Jaccard catches partial word overlap.
    LCS ratio catches shared substrings (e.g. '佛得角门将收' in both titles).
    Taking the max means either signal can trigger a merge."""
    if not a or not b:
        return 0.0

    # Trigram Jaccard
    def trigrams(s: str) -> set[str]:
        if len(s) < 3:
            return {s} if s else set()
        return {s[i : i + 3] for i in range(len(s) - 2)}

    ta, tb = trigrams(a), trigrams(b)
    trig_sim = len(ta & tb) / len(ta | tb) if ta and tb else 0.0

    # Longest common substring ratio (relative to shorter string)
    lcs = _lcs_length(a, b)
    shorter = min(len(a), len(b))
    lcs_sim = lcs / shorter if shorter > 0 else 0.0

    return max(trig_sim, lcs_sim)


def _lcs_length(a: str, b: str) -> int:
    """Length of longest common substring via two-row rolling DP (L9: dedup-quadratic-lcs).

    Uses O(min(m,n)) space instead of O(m×n) by keeping only the previous row."""
    if not a or not b:
        return 0
    # Ensure b is the shorter string for minimal space
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    longest = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                longest = max(longest, curr[j])
        prev = curr
    return longest
