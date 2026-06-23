"""Unit tests for cli._auto_job_id (U5: optional --job-id)."""

from __future__ import annotations

import re

from lcp.cli import _auto_job_id

# YYMMDD-xxxx suffix pattern
_SUFFIX_RE = re.compile(r"\d{6}-[a-z0-9]{4}$")


def test_url_yields_hostname_base():
    result = _auto_job_id(url="https://example.com/article/123")
    assert result.startswith("example-com-")
    assert _SUFFIX_RE.search(result)


def test_url_dots_replaced_with_dashes():
    result = _auto_job_id(url="https://www.news.site.com/path")
    assert ".." not in result and "." not in result
    assert result.startswith("www-news-site-com-")


def test_url_no_hostname_fallback():
    result = _auto_job_id(url="not-a-url")
    assert result.startswith("job-")
    assert _SUFFIX_RE.search(result)


def test_directory_yields_dir_name():
    result = _auto_job_id(directory="/Users/alice/material/my_story")
    assert result.startswith("my-story-")
    assert _SUFFIX_RE.search(result)


def test_neither_url_nor_dir_fallback():
    result = _auto_job_id()
    assert result.startswith("job-")
    assert _SUFFIX_RE.search(result)


def test_max_length_respected():
    # A very long hostname should be truncated to 40 chars, but suffix must survive.
    long_url = "https://this-is-a-very-long-hostname-that-exceeds-the-limit.example.com/"
    a = _auto_job_id(url=long_url)
    b = _auto_job_id(url=long_url)
    assert len(a) <= 40
    assert _SUFFIX_RE.search(a), "random suffix must survive even on max-length ids"
    assert a != b, "truncated ids for the same hostname must still be unique"


def test_two_calls_produce_different_suffixes():
    a = _auto_job_id(url="https://example.com/a")
    b = _auto_job_id(url="https://example.com/a")
    # Same base but different random suffix (may match by luck with 1/36^4 chance)
    assert a[:12] == b[:12]  # same base + YYMMDD
    # suffix parts are 4-char alphanumeric
    assert re.match(r"[a-z0-9]{4}$", a[-4:])
    assert re.match(r"[a-z0-9]{4}$", b[-4:])
    assert a != b, "same id generated twice — randomness is broken"
