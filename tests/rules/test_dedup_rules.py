"""Pure dedup judgement tests — index handed in, zero disk I/O (Unit 6: "純")."""

from __future__ import annotations

from lcp.core.rules import dedup_rules
from lcp.core.rules.dedup_rules import (
    DedupIndex,
    DedupReliability,
    DedupStatus,
    IndexEntry,
    assess_dedup,
    build_queries,
    exact_jaccard,
    normalize_title,
    title_hash,
)

LOREM = (
    "市政府宣布週末將於中央公園舉辦大型美食市集 邀請數十家在地攤商參與 "
    "活動從上午十點持續到晚上八點 並有現場樂團表演"
)


def _index(*entries: IndexEntry, available: bool = True) -> DedupIndex:
    return DedupIndex(entries=tuple(entries), site_index_available=available)


# --- title normalization + hash ----------------------------------------------


def test_normalize_title_strips_punct_case_stopwords():
    assert normalize_title("The Big Event!!!") == "big event"


def test_normalize_title_strips_site_suffix():
    assert normalize_title("重大消息 | ETtoday") == "重大消息"


def test_title_hash_matches_after_normalization():
    assert title_hash("Big Event!") == title_hash("the  big   event")


# --- Stage 1: near-identical title -> duplicate ------------------------------


def test_near_identical_title_is_duplicate():
    idx = _index(IndexEntry(job_id="j1", title="重大火災事件 | ETtoday", body="x"))
    r = assess_dedup(title="重大火災事件！！", body="不同內文", index=idx)
    assert r.status == DedupStatus.DUPLICATE
    assert r.is_duplicate
    assert r.matched_items[0].job_id == "j1"
    assert r.matched_items[0].stage == "title_hash"


# --- Stage 2: body similarity -----------------------------------------------


def test_near_identical_body_is_duplicate():
    idx = _index(IndexEntry(job_id="j2", title="完全不同的標題甲", body=LOREM))
    r = assess_dedup(title="完全不同的標題乙", body=LOREM, index=idx)
    assert r.status == DedupStatus.DUPLICATE
    assert r.matched_items[0].stage == "minhash_lsh"
    assert r.matched_items[0].jaccard is not None


def test_partial_body_overlap_is_uncertain_routes_to_human():
    # Candidate = full index body + a fresh tail -> exact Jaccard lands between
    # the uncertain and duplicate cutoffs. lsh_threshold lowered so the cascade
    # RETRIEVES it as a candidate; the exact re-verify then places it uncertain.
    candidate = LOREM + " 額外新增 一段 完全不同 的 文字 收尾"
    j = exact_jaccard(candidate, LOREM)
    assert 0.15 < j < 0.8  # sanity: this fixture really is in the band
    idx = _index(IndexEntry(job_id="j3", title="某標題", body=LOREM))
    r = assess_dedup(
        title="另一個標題",
        body=candidate,
        index=idx,
        duplicate_jaccard=0.8,
        uncertain_jaccard=0.15,
        lsh_threshold=0.2,
    )
    assert r.status == DedupStatus.UNCERTAIN
    assert r.is_uncertain
    assert r.matched_items[0].jaccard is not None


def test_distinct_content_is_unique_with_index():
    idx = _index(IndexEntry(job_id="j4", title="貓咪展覽開幕", body="可愛的貓咪們齊聚一堂"))
    r = assess_dedup(title="股市今日收紅", body="台股大漲三百點 成交量放大", index=idx)
    assert r.status == DedupStatus.UNIQUE
    assert r.reliability == DedupReliability.HIGH


# --- Honesty / fail-loud: no site index (R36) --------------------------------


def test_no_site_index_low_reliability_not_confident_unique():
    idx = DedupIndex(entries=(), site_index_available=False)
    r = assess_dedup(title="任何標題", body="任何內文", index=idx)
    # NOT unique — fail-loud downgrades to uncertain.
    assert r.status == DedupStatus.UNCERTAIN
    assert r.reliability == DedupReliability.LOW
    assert r.warnings  # carries a warning
    assert "reliability=low" in r.warnings[0]


def test_no_site_index_never_auto_rejects():
    idx = DedupIndex(entries=(), site_index_available=False)
    r = assess_dedup(title="x", body="y", index=idx)
    # uncertain routes to human; it must NOT be a hard duplicate/reject.
    assert r.status != DedupStatus.DUPLICATE


def test_low_reliability_still_reports_real_duplicates():
    # Even with a flaky index flag, a clear title match is reported duplicate
    # (we never SUPPRESS a real hit; we only refuse confident `unique`).
    idx = DedupIndex(
        entries=(IndexEntry(job_id="j5", title="相同標題", body="x"),),
        site_index_available=False,
    )
    r = assess_dedup(title="相同標題", body="y", index=idx)
    assert r.status == DedupStatus.DUPLICATE
    assert r.reliability == DedupReliability.LOW


def test_empty_but_available_index_can_be_confident_unique():
    idx = DedupIndex(entries=(), site_index_available=True)
    r = assess_dedup(title="獨特標題", body="獨特內文", index=idx)
    assert r.status == DedupStatus.UNIQUE
    assert r.reliability == DedupReliability.HIGH


# --- Query groups (R21) ------------------------------------------------------


def test_build_queries_two_groups():
    qs = build_queries(
        person_or_account="某網紅",
        event="爆料事件",
        place_or_platform_or_school="某平台",
        core_event="直播衝突",
    )
    assert len(qs) == 2
    assert qs[0].group == "person_account_event"
    assert qs[0].terms == ["某網紅", "爆料事件"]
    assert qs[1].group == "place_platform_school_event"
    assert qs[1].terms == ["某平台", "直播衝突"]


def test_build_queries_drops_empty_terms_keeps_groups():
    qs = build_queries(person_or_account="只有人名")
    assert len(qs) == 2  # both groups present even if partial
    assert qs[0].terms == ["只有人名"]
    assert qs[1].terms == []


def test_queries_threaded_into_result():
    idx = _index(IndexEntry(job_id="j", title="abc", body="def"))
    qs = build_queries(person_or_account="p", event="e")
    r = assess_dedup(title="zzz", body="qqq", index=idx, queries=qs)
    assert r.queries == qs


# --- exact Jaccard helper ----------------------------------------------------


def test_exact_jaccard_identical_is_one():
    assert exact_jaccard("a b c d e", "a b c d e") == 1.0


def test_exact_jaccard_disjoint_is_zero():
    assert exact_jaccard("a b c d", "w x y z") == 0.0
