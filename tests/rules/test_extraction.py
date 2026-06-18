"""Pure content-extraction policy (U4) — no scrapy, fabricated Response.

The second-order SSRF check is injected, so these prove the extraction logic
(title/body fallback, classify, de-dupe, reject-partition) AND that an unsafe
media URL is dropped to rejected_media_urls via the injected callback."""

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
