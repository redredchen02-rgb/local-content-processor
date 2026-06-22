"""Tests for the Douyin hot-search scraper.

No pytest-asyncio in this project, so async fetch() is driven via asyncio.run();
httpx is mocked by monkeypatching the module's AsyncClient with a fake async
context-manager client (the lcp LLM client mocks at the SDK layer, which doesn't
apply to these raw-httpx scrapers)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gossip_scraper.scrapers import douyin
from gossip_scraper.scrapers.douyin import DouyinScraper


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx.HTTPStatusError(
                f"{self._status}",
                request=httpx.Request("GET", douyin._DOUYIN_HOT),
                response=httpx.Response(self._status),
            )

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, *a: object, **k: object) -> _FakeResp:
        return _FakeResp(self._payload, self._status)


def _patch(monkeypatch, payload: dict, status: int = 200) -> None:
    monkeypatch.setattr(
        douyin.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(payload, status),
    )


_SAMPLE = {
    "word_list": [
        {"word": "某明星塌房", "hot_value": 9999999, "label": 3},
        {"word": "某综艺爆料", "hot_value": 5000000, "label": 1},
        {"word": "普通热搜", "hot_value": 100000, "label": 0},
    ]
}


def test_parses_hot_search(monkeypatch) -> None:
    _patch(monkeypatch, _SAMPLE)
    items = asyncio.run(DouyinScraper().fetch(limit=10))
    assert len(items) == 3
    assert all(it.platform == "douyin" for it in items)
    assert [it.rank for it in items] == [1, 2, 3]
    assert items[0].title == "某明星塌房"
    assert items[0].heat == 9999999
    assert items[0].url.startswith("https://www.douyin.com/search/")
    assert items[0].tag == "热"  # label 3
    assert items[1].tag == "新"  # label 1
    assert items[2].tag == ""  # label 0 -> untagged


def test_empty_word_list_returns_empty(monkeypatch) -> None:
    _patch(monkeypatch, {"word_list": []})
    assert asyncio.run(DouyinScraper().fetch()) == []


def test_missing_word_list_key_returns_empty(monkeypatch) -> None:
    _patch(monkeypatch, {"status_code": 0})
    assert asyncio.run(DouyinScraper().fetch()) == []


def test_http_error_raises(monkeypatch) -> None:
    # Sibling convention: the scraper RAISES on HTTP error; run()/_fetch_one
    # turns that into a clean per-platform miss + a recorded health failure.
    _patch(monkeypatch, {}, status=503)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(DouyinScraper().fetch())


def test_limit_truncates(monkeypatch) -> None:
    _patch(monkeypatch, _SAMPLE)
    items = asyncio.run(DouyinScraper().fetch(limit=2))
    assert len(items) == 2


def test_missing_fields_tolerated(monkeypatch) -> None:
    # An entry with no hot_value/label must not crash; heat defaults to 0.
    _patch(monkeypatch, {"word_list": [{"word": "只有标题"}]})
    items = asyncio.run(DouyinScraper().fetch())
    assert len(items) == 1
    assert items[0].title == "只有标题"
    assert items[0].heat == 0
    assert items[0].tag == ""
