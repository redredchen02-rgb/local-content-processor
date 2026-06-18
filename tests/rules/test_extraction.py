"""Pure content-extraction policy (U4) — no scrapy, fabricated Response.

The second-order SSRF check is injected, so these prove the extraction logic
(title/body fallback, classify, de-dupe, reject-partition) AND that an unsafe
media URL is dropped to rejected_media_urls via the injected callback."""

import urllib.parse

from lcp.core.models import AssetKind
from lcp.core.rules.extraction import classify_media_url, extract_content


class _Sel:
    def __init__(self, values):
        self._v = values

    def get(self):
        return self._v[0] if self._v else None

    def getall(self):
        return list(self._v)


class _FakeResponse:
    """Duck-typed Scrapy Response: css() returns are pre-canned per selector."""

    def __init__(self, css_map, *, url="https://ex.com/a", text="<html>x</html>"):
        self._css = css_map
        self.url = url
        self.text = text
        self.status = 200

    def css(self, selector):
        return _Sel(self._css.get(selector, []))

    def urljoin(self, u):
        return u if u.startswith("http") else "https://ex.com/" + u.lstrip("/")


class _RealUrljoinResponse(_FakeResponse):
    """Uses the REAL stdlib urljoin so a malformed bracketed-IPv6 host raises
    ValueError exactly as in production. The default _FakeResponse.urljoin is a
    passthrough that hides this crash class entirely (the bug U1 fixes)."""

    def urljoin(self, u):
        return urllib.parse.urljoin(self.url, u)


def _always_safe(_u):
    return True


def test_classify_media_url():
    assert classify_media_url("https://x/a.JPG") is AssetKind.IMAGE
    assert classify_media_url("https://x/a.mp4?t=1") is AssetKind.VIDEO
    assert classify_media_url("https://x/page.html") is None


def test_title_falls_back_to_h1():
    resp = _FakeResponse({"title::text": [], "h1::text": ["Heading"], "p::text": ["b"]})
    out = extract_content(resp, is_media_url_safe=_always_safe)
    assert out["title"] == "Heading"


def test_body_prefers_article_then_p():
    resp = _FakeResponse(
        {
            "title::text": ["T"],
            "article p::text, main p::text": ["one", " two "],
            "p::text": ["ignored"],
        }
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    assert out["body"] == "one\ntwo"


def test_media_de_duped():
    resp = _FakeResponse(
        {"title::text": ["T"], "img::attr(src)": ["https://ex.com/a.jpg"] * 2}
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    assert out["image_urls"] == ["https://ex.com/a.jpg"]


def test_injected_ssrf_check_drops_unsafe_media_to_rejected():
    blocked = {"https://169.254.169.254/meta.jpg"}
    resp = _FakeResponse(
        {
            "title::text": ["T"],
            "img::attr(src)": [
                "https://ex.com/ok.jpg",
                "https://169.254.169.254/meta.jpg",
            ],
        }
    )
    out = extract_content(resp, is_media_url_safe=lambda u: u not in blocked)
    assert out["image_urls"] == ["https://ex.com/ok.jpg"]
    assert out["rejected_media_urls"] == ["https://169.254.169.254/meta.jpg"]


def test_links_to_media_are_classified_and_accepted():
    resp = _FakeResponse(
        {
            "title::text": ["T"],
            "a::attr(href)": ["https://ex.com/clip.mp4", "https://ex.com/page.html"],
        }
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    assert out["video_urls"] == ["https://ex.com/clip.mp4"]
    assert out["image_urls"] == []


def test_malformed_img_url_does_not_abort_extraction():
    """U1 (P0): a single malformed bracketed-IPv6 <img src> must not raise out
    of extract_content and discard the whole page. Uses the REAL urljoin."""
    resp = _RealUrljoinResponse(
        {
            "title::text": ["Real Title"],
            "p::text": ["real body"],
            "img::attr(src)": [
                "http://[::bad::]/x.jpg",  # malformed host -> real urljoin raises
                "https://ex.com/ok.jpg",
            ],
        }
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    # The page's real content survives; the bad URL is dropped, not fatal.
    assert out["title"] == "Real Title"
    assert out["body"] == "real body"
    assert "https://ex.com/ok.jpg" in out["image_urls"]
    assert all("bad" not in u for u in out["image_urls"])
    # bug_005: a parse failure is recorded SEPARATELY from SSRF rejections, so the
    # adapter can stamp a truthful per-reason note. The malformed src lands in
    # malformed_media_urls, never in the SSRF-only rejected_media_urls.
    assert any("[::bad::]" in u for u in out["malformed_media_urls"])
    assert out["rejected_media_urls"] == []


def test_malformed_video_url_does_not_abort_extraction():
    resp = _RealUrljoinResponse(
        {
            "title::text": ["T"],
            "p::text": ["b"],
            "video::attr(src), video source::attr(src)": [
                "http://[1:2:3]/z.mp4",
                "https://ex.com/ok.mp4",
            ],
        }
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    assert out["video_urls"] == ["https://ex.com/ok.mp4"]
    assert out["body"] == "b"
    # bug_005: malformed -> malformed_media_urls (parse failure), not the SSRF list.
    assert any("[1:2:3]" in u for u in out["malformed_media_urls"])
    assert out["rejected_media_urls"] == []


def test_malformed_link_href_is_skipped_not_fatal():
    resp = _RealUrljoinResponse(
        {
            "title::text": ["T"],
            "p::text": ["b"],
            "a::attr(href)": ["http://[:::]/y.mp4", "https://ex.com/clip.mp4"],
        }
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    # The valid media link is still classified; the malformed one is skipped.
    assert out["video_urls"] == ["https://ex.com/clip.mp4"]


def test_same_url_appearing_as_both_img_and_video_is_deduped():
    """CORE-2: _accept must check ALL lists, not just the same-kind list.
    A URL that first appears as an image must not be re-added as a video."""
    shared_url = "https://ex.com/ambiguous.mp4"
    resp = _FakeResponse(
        {
            "title::text": ["T"],
            "p::text": ["body"],
            "img::attr(src)": [shared_url],
            "video::attr(src), video source::attr(src)": [shared_url],
            "a::attr(href)": [],
        }
    )
    out = extract_content(resp, is_media_url_safe=_always_safe)
    # The URL appeared as img first, so it should be in image_urls only.
    total = len(out["image_urls"]) + len(out["video_urls"])
    assert total == 1, (
        f"URL appeared in both image_urls and video_urls: "
        f"images={out['image_urls']}, videos={out['video_urls']}"
    )
