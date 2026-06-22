"""Pure content-extraction policy from a crawled Response (plan 004 U4).

Moved out of `adapters/crawler/scrapy_impl.py` so the extraction *judgement*
(title/body fallback, media-URL classification + de-dupe) is strict-checked and
unit-testable without scrapy. The SECOND-ORDER SSRF check (net_guard DNS) is I/O
and stays in the adapter — it is injected here as ``is_media_url_safe``, so this
module imports nothing from adapters and performs no I/O of its own. The DNS
check is the only judgement that reaches the network, and it lives in the
caller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..models import AssetKind

# Image/video extensions used when classifying scraped media URLs.
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v", ".mkv")


def _safe_urljoin(response: Any, raw: str) -> str | None:
    """``response.urljoin`` raises ``ValueError`` on a malformed host (a bracketed
    IPv6 literal like ``http://[::bad::]/x`` — stdlib ``urlsplit`` validates it via
    ``ipaddress``). A single bad URL on an allowlisted page must NOT abort the whole
    page's extraction, so return ``None`` and let the caller drop it."""
    try:
        joined: str = response.urljoin(raw)
    except ValueError:
        return None
    return joined


def classify_media_url(url: str) -> AssetKind | None:
    low = url.lower().split("?", 1)[0]
    if low.endswith(_IMAGE_EXT):
        return AssetKind.IMAGE
    if low.endswith(_VIDEO_EXT):
        return AssetKind.VIDEO
    return None


def extract_content(response: Any, *, is_media_url_safe: Callable[[str], bool]) -> dict[str, Any]:
    """Pure extraction from a crawl Response. Returns title, body text,
    image_urls, video_urls, rejected_media_urls, malformed_media_urls,
    source_html, and metadata.

    De-dupes media URLs AND runs every scraped media URL through the injected
    ``is_media_url_safe`` (the adapter's second-order SSRF guard): a URL pointing
    at an internal/metadata IP is NOT added to the download lists; it is recorded
    in ``rejected_media_urls`` so write_bundle records it as a FAILED asset. A URL
    that does not even parse (malformed host) is recorded separately in
    ``malformed_media_urls`` — it never reached the SSRF preflight, so labelling it
    an SSRF rejection in the manifest would be untrue (bug_005)."""
    title = (response.css("title::text").get() or "").strip()
    if not title:
        title = (response.css("h1::text").get() or "").strip()

    # Body text: prefer <article>/<main>, else all <p>.
    paras = response.css("article p::text, main p::text").getall()
    if not paras:
        paras = response.css("p::text").getall()
    body = "\n".join(t.strip() for t in paras if t.strip()).strip()

    image_urls: list[str] = []
    video_urls: list[str] = []
    rejected_media_urls: list[str] = []  # dropped by the second-order SSRF guard
    malformed_media_urls: list[str] = []  # dropped because the URL would not parse

    def _accept(full: str, kind: AssetKind) -> None:
        target = image_urls if kind is AssetKind.IMAGE else video_urls
        # De-dupe across ALL lists (not just same-kind): a URL that already
        # appears as image, video, rejected, or malformed must not be re-added.
        if (
            full in image_urls
            or full in video_urls
            or full in rejected_media_urls
            or full in malformed_media_urls
        ):
            return
        if not is_media_url_safe(full):
            rejected_media_urls.append(full)  # second-order SSRF -> drop
            return
        target.append(full)

    def _malformed(src: str) -> None:
        # A parse failure is NOT an SSRF rejection (it never reached the preflight),
        # so record it separately — the adapter stamps a truthful per-reason note.
        if src not in malformed_media_urls:
            malformed_media_urls.append(src)

    for src in response.css("img::attr(src)").getall():
        full = _safe_urljoin(response, src)
        if full is None:
            _malformed(src)  # malformed URL -> drop, record, never fatal
            continue
        _accept(full, AssetKind.IMAGE)

    for src in response.css("video::attr(src), video source::attr(src)").getall():
        full = _safe_urljoin(response, src)
        if full is None:
            _malformed(src)
            continue
        _accept(full, AssetKind.VIDEO)

    # Also classify links pointing at media files. A malformed href is skipped
    # (it may not be media at all), never fatal.
    for href in response.css("a::attr(href)").getall():
        full = _safe_urljoin(response, href)
        if full is None:
            continue
        kind = classify_media_url(full)
        if kind is not None:
            _accept(full, kind)

    return {
        "title": title,
        "body": body,
        "image_urls": image_urls,
        "video_urls": video_urls,
        "rejected_media_urls": rejected_media_urls,
        "malformed_media_urls": malformed_media_urls,
        "source_html": response.text,
        "metadata": {
            "url": response.url,
            "status": getattr(response, "status", None),
        },
    }
