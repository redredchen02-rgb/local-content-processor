"""Tests for the gossip batch-injection adapter + Pipeline.ingest_gossip (U4).

Covers the source-URL persistence seam that resolves the review-P0 ingest→crawl
gap: each item becomes a NEW job whose source URL lives in the PII-bearing job
bundle (source.json), readable back by job_id."""

from __future__ import annotations

import pytest

from lcp.adapters.storage import gossip_ingest as gi
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import Config
from lcp.core.errors import InputValidationError
from lcp.core.state import JobState
from lcp.pipeline import Pipeline

TS = "2026-06-22T00:00:00Z"


def _store(tmp_path) -> JobStore:
    return JobStore(base_dir=tmp_path / "data")


def _pipeline(tmp_path) -> Pipeline:
    store = _store(tmp_path)
    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    return Pipeline(Config(), store, audit)


def _items(*urls: str) -> list[dict[str, object]]:
    return [
        {"platform": "weibo", "rank": i + 1, "title": f"瓜{i}", "url": u}
        for i, u in enumerate(urls)
    ]


# --- adapter: ingest_items -------------------------------------------------


def test_creates_one_new_job_per_item(tmp_path) -> None:
    store = _store(tmp_path)
    report = gi.ingest_items(
        _items("https://s.weibo.com/weibo?q=a", "https://www.douyin.com/search/b"),
        store,
        ts=TS,
    )
    assert len(report.created) == 2
    assert report.skipped == []
    for jid in report.created:
        rec = store.get_job(jid)
        assert rec is not None and rec.state is JobState.NEW
        assert gi.read_source_url(store.job_dir(jid))  # persisted + readable


def test_source_url_round_trips(tmp_path) -> None:
    store = _store(tmp_path)
    url = "https://s.weibo.com/weibo?q=吴磊"
    report = gi.ingest_items([{"platform": "weibo", "title": "吴磊", "url": url}], store, ts=TS)
    assert gi.read_source_url(store.job_dir(report.created[0])) == url


def test_read_source_url_missing_returns_none(tmp_path) -> None:
    store = _store(tmp_path)
    store.ensure_job_dir("plain-job")  # no source.json
    assert gi.read_source_url(store.job_dir("plain-job")) is None


def test_read_source_url_non_utf8_returns_none(tmp_path) -> None:
    # A corrupt / non-UTF-8 source.json must honor the "malformed -> None"
    # contract (UnicodeDecodeError is a ValueError, not OSError) — never crash run.
    store = _store(tmp_path)
    store.ensure_job_dir("corrupt")
    (store.job_dir("corrupt") / gi.SOURCE_NAME).write_bytes(b"\xff\xfe\x00not-utf8")
    assert gi.read_source_url(store.job_dir("corrupt")) is None


def test_tampered_internal_url_rejected_by_crawl_guard(tmp_path) -> None:
    # A source.json tampered on disk to an internal URL is read back verbatim
    # (ingest's cheap scheme check passed), but the crawl-time SSRF guard rejects
    # it — so the deferred-crawl seam cannot become an SSRF bypass.
    from lcp.adapters.crawler import net_guard

    store = _store(tmp_path)
    store.ensure_job_dir("tampered")
    gi.write_source(
        store.job_dir("tampered"), url="http://127.0.0.1/x", platform="weibo", title="t"
    )
    url = gi.read_source_url(store.job_dir("tampered"))
    assert url == "http://127.0.0.1/x"  # read back as-is
    with pytest.raises(Exception):
        net_guard.validate_url(url)  # loopback IP rejected at crawl preflight


def test_empty_list(tmp_path) -> None:
    report = gi.ingest_items([], _store(tmp_path), ts=TS)
    assert report.created == [] and report.skipped == []


def test_skips_invalid_or_empty_url_non_lossy(tmp_path) -> None:
    store = _store(tmp_path)
    items: list[dict[str, object]] = [
        {"platform": "weibo", "title": "ok", "url": "https://s.weibo.com/x"},
        {"platform": "weibo", "title": "bad-scheme", "url": "ftp://evil/x"},
        {"platform": "weibo", "title": "empty", "url": ""},
        {"platform": "weibo", "title": "no-url"},
    ]
    report = gi.ingest_items(items, store, ts=TS)
    assert len(report.created) == 1
    assert len(report.skipped) == 3  # non-lossy: every bad item reported
    assert all(s["reason"] == "invalid_or_empty_url" for s in report.skipped)
    assert {s["title"] for s in report.skipped} == {"bad-scheme", "empty", "no-url"}


def test_skips_missing_required_fields(tmp_path) -> None:
    report = gi.ingest_items([{"url": "https://s.weibo.com/x"}], _store(tmp_path), ts=TS)
    assert report.created == []
    assert report.skipped[0]["reason"] == "missing_fields"


def test_dedups_within_batch_by_url(tmp_path) -> None:
    store = _store(tmp_path)
    url = "https://s.weibo.com/weibo?q=same"
    items: list[dict[str, object]] = [
        {"platform": "weibo", "title": "a", "url": url},
        {"platform": "douyin", "title": "b", "url": url},
    ]
    report = gi.ingest_items(items, store, ts=TS)
    assert len(report.created) == 1
    assert report.skipped[0]["reason"] == "duplicate_in_batch"


def test_idempotent_reingest_reports_already_exists(tmp_path) -> None:
    store = _store(tmp_path)
    items = _items("https://s.weibo.com/weibo?q=x")
    r1 = gi.ingest_items(items, store, ts=TS)
    r2 = gi.ingest_items(items, store, ts=TS)
    assert len(r1.created) == 1
    assert r2.created == []
    assert r2.skipped[0]["reason"] == "already_exists"
    assert r2.skipped[0]["job_id"] == r1.created[0]


def test_oversized_batch_refused(tmp_path) -> None:
    store = _store(tmp_path)
    items = _items(*[f"https://s.weibo.com/q={i}" for i in range(5)])
    with pytest.raises(InputValidationError):
        gi.ingest_items(items, store, ts=TS, max_items=3)
    # the refusal is before the loop -> no jobs created
    assert store.get_job(gi.make_job_id("weibo", "https://s.weibo.com/q=0")) is None


# --- adapter: parse_payload + make_job_id ----------------------------------


def test_parse_payload_valid() -> None:
    items = gi.parse_payload('[{"platform":"weibo","title":"t","url":"https://x"}]')
    assert len(items) == 1 and items[0]["platform"] == "weibo"


@pytest.mark.parametrize("bad", ["not json", '{"platform":"weibo"}', "[1, 2, 3]"])
def test_parse_payload_rejects_malformed(bad: str) -> None:
    with pytest.raises(InputValidationError):
        gi.parse_payload(bad)


def test_make_job_id_deterministic_and_url_sensitive() -> None:
    a = gi.make_job_id("weibo", "https://x")
    b = gi.make_job_id("weibo", "https://x")
    c = gi.make_job_id("weibo", "https://y")
    assert a == b and a != c
    assert a.startswith("gossip-weibo-")


# --- Pipeline.ingest_gossip wrapper ----------------------------------------


def test_pipeline_ingest_gossip_wrapper(tmp_path) -> None:
    p = _pipeline(tmp_path)
    report = p.ingest_gossip(_items("https://s.weibo.com/weibo?q=z"), ts=TS)
    assert len(report.created) == 1
    d = report.to_dict()
    assert d["created_count"] == 1 and d["skipped_count"] == 0
