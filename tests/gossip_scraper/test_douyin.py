"""Tests for the Douyin hot-search scraper.

No pytest-asyncio in this project, so async fetch() is driven via asyncio.run();
fetch_json is mocked via monkeypatch on the base module."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gossip_scraper.scrapers import douyin
from gossip_scraper.scrapers.douyin import DouyinScraper


def _patch(monkeypatch, payload: dict, status: int = 200) -> None:
    if status >= 400:

        async def _fail(*a: object, **k: object) -> dict:
            raise httpx.HTTPStatusError(
                f"{status}",
                request=httpx.Request("GET", "https://www.douyin.com"),
                response=httpx.Response(status),
            )

        monkeypatch.setattr(douyin, "fetch_json", _fail)
    else:

        async def _ok(*a: object, **k: object) -> dict:
            return payload

        monkeypatch.setattr(douyin, "fetch_json", _ok)


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
    _patch(monkeypatch, {}, status=503)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(DouyinScraper().fetch())


def test_limit_truncates(monkeypatch) -> None:
    _patch(monkeypatch, _SAMPLE)
    items = asyncio.run(DouyinScraper().fetch(limit=2))
    assert len(items) == 2


def test_missing_fields_tolerated(monkeypatch) -> None:
    _patch(monkeypatch, {"word_list": [{"word": "只有标题"}]})
    items = asyncio.run(DouyinScraper().fetch())
    assert len(items) == 1
    assert items[0].title == "只有标题"
    assert items[0].heat == 0
    assert items[0].tag == ""
