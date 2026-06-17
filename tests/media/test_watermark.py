"""Unit 1: official-watermark ADD primitive + normalizer wiring."""

from __future__ import annotations

import pytest
from PIL import Image

from lcp.adapters.media import normalizer
from lcp.adapters.media.watermark import add_watermark
from lcp.core.config import WatermarkConfig
from lcp.core.errors import InputValidationError


def _logo(path, size=(40, 40), color=(255, 0, 0, 255)):
    Image.new("RGBA", size, color).save(path, format="PNG")
    return str(path)


def _base(size=(800, 450), color=(0, 0, 0)):
    return Image.new("RGB", size, color)


# --- logo mode ----------------------------------------------------------------


def test_logo_watermark_bottom_right_is_rgb_with_mark(tmp_path):
    cfg = WatermarkConfig(
        enabled=True, mode="logo", logo_body_path=_logo(tmp_path / "logo.png"),
        position="bottom-right", opacity=1.0, margin=10,
    )
    out = add_watermark(_base(), cfg, kind="body")
    assert out.mode == "RGB"
    # bottom-right corner carries the red mark; top-left stays black.
    assert out.getpixel((780, 430))[0] > 150
    assert out.getpixel((5, 5)) == (0, 0, 0)


def test_cover_kind_uses_cover_asset(tmp_path):
    cfg = WatermarkConfig(
        enabled=True, mode="logo",
        logo_body_path=_logo(tmp_path / "b.png", color=(0, 255, 0, 255)),
        logo_cover_path=_logo(tmp_path / "c.png", color=(255, 0, 0, 255)),
        position="top-left", opacity=1.0, margin=5,
    )
    out = add_watermark(Image.new("RGB", (1300, 640)), cfg, kind="cover")
    # top-left carries the cover (red) asset, not the body (green) one.
    assert out.getpixel((10, 10))[0] > 150


# --- text mode ----------------------------------------------------------------


def test_text_watermark_renders_pixels(tmp_path):
    cfg = WatermarkConfig(
        enabled=True, mode="text", text="EATMELON", position="bottom-right",
        opacity=1.0, color=(255, 255, 255),
    )
    out = add_watermark(_base(), cfg, kind="body")
    assert out.mode == "RGB"
    # some non-black pixels exist (the glyphs); fully black means nothing drawn.
    assert out.getextrema() != ((0, 0), (0, 0), (0, 0))


def test_empty_text_rejected():
    cfg = WatermarkConfig(enabled=True, mode="text", text="   ")
    with pytest.raises(InputValidationError):
        add_watermark(_base(), cfg, kind="body")


# --- edge / error paths -------------------------------------------------------


def test_rgba_source_saves_as_rgb(tmp_path):
    cfg = WatermarkConfig(
        enabled=True, mode="logo", logo_body_path=_logo(tmp_path / "l.png"),
    )
    out = add_watermark(Image.new("RGBA", (800, 450)), cfg, kind="body")
    assert out.mode == "RGB"  # JPEG-writable, no "cannot write mode RGBA"


def test_oversized_logo_is_clamped_no_crash(tmp_path):
    big = _logo(tmp_path / "huge.png", size=(4000, 4000))
    cfg = WatermarkConfig(enabled=True, mode="logo", logo_body_path=big, margin=0)
    out = add_watermark(_base(size=(200, 200)), cfg, kind="body")
    # clamped to <= half the base, so the top-left quadrant is untouched.
    assert out.getpixel((5, 5)) == (0, 0, 0)


def test_missing_logo_asset_raises(tmp_path):
    cfg = WatermarkConfig(
        enabled=True, mode="logo", logo_body_path=str(tmp_path / "nope.png"),
    )
    with pytest.raises(InputValidationError):
        add_watermark(_base(), cfg, kind="body")


def test_logo_mode_without_asset_raises():
    cfg = WatermarkConfig(enabled=True, mode="logo")
    with pytest.raises(InputValidationError):
        add_watermark(_base(), cfg, kind="body")


def test_bad_kind_and_position_rejected(tmp_path):
    cfg = WatermarkConfig(enabled=True, mode="text", text="x")
    with pytest.raises(InputValidationError):
        add_watermark(_base(), cfg, kind="banner")
    cfg2 = WatermarkConfig(enabled=True, mode="text", text="x", position="middle")
    with pytest.raises(InputValidationError):
        add_watermark(_base(), cfg2, kind="body")


# --- normalizer integration ---------------------------------------------------


def _sharp_jpeg(path, size=(1000, 600)):
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            v = 255 if (x + y) % 2 == 0 else 0
            px[x, y] = (v, v, v)
    img.save(path, format="JPEG", quality=95)
    return str(path)


def test_normalize_applies_watermark_when_enabled(tmp_path):
    src = _sharp_jpeg(tmp_path / "in.jpg")
    cfg = WatermarkConfig(
        enabled=True, mode="logo",
        logo_body_path=_logo(tmp_path / "logo.png"),
        position="bottom-right", opacity=1.0, margin=8,
    )
    res = normalizer.normalize_image(src, tmp_path / "out.jpg", watermark=cfg)
    out = Image.open(res.out_path)
    assert out.mode == "RGB"
    assert out.getpixel((out.width - 6, out.height - 6))[0] > 120


def test_normalize_without_watermark_unchanged(tmp_path):
    src = _sharp_jpeg(tmp_path / "in.jpg")
    res = normalizer.normalize_image(src, tmp_path / "out.jpg")
    assert Image.open(res.out_path).mode == "RGB"


def test_disabled_watermark_is_noop(tmp_path):
    src = _sharp_jpeg(tmp_path / "in.jpg")
    cfg = WatermarkConfig(enabled=False, mode="text", text="x")
    res = normalizer.normalize_image(src, tmp_path / "out.jpg", watermark=cfg)
    assert Image.open(res.out_path).mode == "RGB"


def test_cover_watermarked_once_after_compose(tmp_path):
    a = _sharp_jpeg(tmp_path / "a.jpg")
    b = _sharp_jpeg(tmp_path / "b.jpg")
    cfg = WatermarkConfig(
        enabled=True, mode="logo",
        logo_cover_path=_logo(tmp_path / "c.png", color=(255, 0, 0, 255)),
        position="bottom-right", opacity=1.0, margin=10,
    )
    out_path = normalizer.make_cover([a, b], tmp_path / "cover.jpg", watermark=cfg)
    cover = Image.open(out_path)
    assert cover.size == (1300, 640)
    assert cover.getpixel((1285, 625))[0] > 120
