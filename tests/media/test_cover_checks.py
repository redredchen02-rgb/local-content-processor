"""Unit 2: cover measurement adapter + safe-area preview."""

from __future__ import annotations

import pytest
from PIL import Image

from lcp.adapters.media import cover_checks, normalizer
from lcp.core.errors import InputValidationError


def _noise(path, size=(1300, 640)):
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            v = 255 if (x + y) % 3 == 0 else 0
            px[x, y] = (v, v, v)
    img.save(path, format="JPEG", quality=95)
    return str(path)


def _flat(path, size=(1300, 640), color=(120, 120, 120)):
    Image.new("RGB", size, color).save(path, format="JPEG", quality=95)
    return str(path)


def test_cover_cell_rects_match_layout():
    rects = normalizer.cover_cell_rects(2, 1300, 640)
    assert len(rects) == 2
    assert rects[0][0] == 0  # first tile starts at left edge


def test_evaluate_flat_cover_is_clean(tmp_path):
    cover = _flat(tmp_path / "cover.jpg")
    adv = cover_checks.evaluate_cover(cover, 1, cover_width=1300, cover_height=640)
    # a uniform grey: no border (mid mean), low entropy, balanced energy
    assert not adv.has_any


def test_evaluate_cover_with_black_border_warns(tmp_path):
    img = Image.new("RGB", (1300, 640), (120, 120, 120))
    # paint a flat black bar down the left 8px strip
    for y in range(640):
        for x in range(8):
            img.putpixel((x, y), (0, 0, 0))
    p = str(tmp_path / "cover.jpg")
    img.save(p, format="JPEG", quality=95)
    adv = cover_checks.evaluate_cover(p, 1, cover_width=1300, cover_height=640)
    assert any("left" in g for g in adv.geometry)


def test_large_cover_analyzed_on_bounded_working_size(tmp_path, monkeypatch):
    # A legal-size cover well above the working cap (but under the 50 MP bomb
    # cap) must be analyzed on the downscaled frame, not the full resolution,
    # so worst-case in-process CPU is bounded. We capture the size the edge-split
    # measurement actually receives.
    seen_sizes: list[tuple[int, int]] = []
    real_split = cover_checks._edge_energy_split

    def spy(gray):
        seen_sizes.append(gray.size)
        return real_split(gray)

    monkeypatch.setattr(cover_checks, "_edge_energy_split", spy)

    # 3000x3000 = 9 MP, comfortably above the 4 MP working cap and below 50 MP.
    big = Image.new("RGB", (3000, 3000), (120, 120, 120))
    p = str(tmp_path / "big_cover.jpg")
    big.save(p, format="JPEG", quality=95)

    cover_checks.evaluate_cover(p, 1, cover_width=1300, cover_height=640)

    assert seen_sizes, "edge-split measurement was not invoked"
    w, h = seen_sizes[0]
    assert w * h <= normalizer.COVER_ANALYSIS_MAX_PIXELS  # analyzed downscaled
    assert (w, h) != (3000, 3000)  # not the full frame


def test_oversize_cover_still_refused_by_bomb_guard(tmp_path, monkeypatch):
    # The decompression-bomb guard is unchanged: an image over the pixel cap is
    # refused before any analysis runs (here the cap is lowered for speed).
    monkeypatch.setattr(normalizer.Image, "MAX_IMAGE_PIXELS", 1000)
    img = Image.new("RGB", (200, 200), (120, 120, 120))  # 40k px > 1000 cap
    p = str(tmp_path / "bomb.png")
    img.save(p, format="PNG")
    with pytest.raises(InputValidationError) as exc:
        cover_checks.evaluate_cover(p, 1, cover_width=1300, cover_height=640)
    assert "bomb" in str(exc.value).lower()


def test_safe_area_preview_is_written(tmp_path):
    cover = _noise(tmp_path / "cover.jpg")
    out = cover_checks.write_safe_area_preview(
        cover, tmp_path / "preview.jpg", cover_width=1300, cover_height=640
    )
    preview = Image.open(out)
    assert preview.size == (1300, 640)
    assert preview.mode == "RGB"
