"""Pillow normalizer tests. Test images are generated at runtime (no binaries
committed)."""

from __future__ import annotations

import warnings

import pytest
from PIL import Image

from lcp.core.errors import InputValidationError
from lcp.core.models import AssetState
from lcp.adapters.media import normalizer


def _save_rgb(path, size, color=(120, 60, 30)):
    Image.new("RGB", size, color).save(path, format="JPEG", quality=95)
    return path


def _save_noise(path, size):
    """A high-frequency checkerboard -> high Laplacian variance (sharp)."""
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            v = 255 if (x + y) % 2 == 0 else 0
            px[x, y] = (v, v, v)
    img.save(path, format="PNG")
    return path


# --- happy path ---------------------------------------------------------------


def test_normalize_scales_to_max_width_proportional(tmp_path):
    src = _save_rgb(tmp_path / "big.jpg", (2000, 1000))
    dst = tmp_path / "out.jpg"
    res = normalizer.normalize_image(src, dst, max_width=800)
    assert res.width == 800
    assert res.height == 400  # 2:1 ratio preserved
    assert (tmp_path / "out.jpg").exists()


def test_normalize_does_not_upscale_small_image(tmp_path):
    src = _save_rgb(tmp_path / "small.jpg", (700, 700))
    dst = tmp_path / "out.jpg"
    res = normalizer.normalize_image(src, dst, max_width=800)
    assert res.width == 700  # thumbnail only shrinks


def test_normalize_output_is_jpeg(tmp_path):
    src = _save_rgb(tmp_path / "in.jpg", (1200, 800))
    dst = tmp_path / "out.jpg"
    normalizer.normalize_image(src, dst)
    with Image.open(dst) as im:
        assert im.format == "JPEG"


# --- blur decision (variance fed to pure rules) -------------------------------


def test_sharp_image_passes(tmp_path):
    src = _save_noise(tmp_path / "sharp.png", (800, 600))
    dst = tmp_path / "out.jpg"
    res = normalizer.normalize_image(src, dst, blur_threshold=10.0)
    assert res.laplacian_variance > 10.0
    assert res.decision.state == AssetState.OK


def test_flat_image_is_blurry_needs_revision(tmp_path):
    # Solid color -> ~zero edge energy -> low variance -> needs_revision.
    # Saved as PNG (lossless): a JPEG roundtrip would inject 8x8 DCT block
    # artifacts and inflate the variance, so use lossless input here.
    src = tmp_path / "flat.png"
    Image.new("RGB", (800, 600), (128, 128, 128)).save(src, format="PNG")
    dst = tmp_path / "out.jpg"
    res = normalizer.normalize_image(src, dst, blur_threshold=50.0)
    assert res.laplacian_variance < 50.0
    assert res.decision.state == AssetState.NEEDS_REVISION
    assert any("blurry" in r for r in res.decision.reasons)


def test_too_small_image_needs_revision(tmp_path):
    src = _save_noise(tmp_path / "tiny.png", (100, 100))
    dst = tmp_path / "out.jpg"
    res = normalizer.normalize_image(
        src, dst, min_width=640, min_height=360, blur_threshold=0.0
    )
    assert res.decision.state == AssetState.NEEDS_REVISION
    assert any("too small" in r for r in res.decision.reasons)


# --- EXIF orientation ---------------------------------------------------------


def test_exif_transpose_applied(tmp_path):
    # Make a wide image, tag it as orientation 6 (rotate 90deg CW on display).
    # After exif_transpose the stored portrait becomes the intended landscape.
    img = Image.new("RGB", (400, 200), (10, 20, 30))
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation tag
    # Save physically rotated so the EXIF flag is meaningful: store portrait.
    portrait = img.rotate(-90, expand=True)  # now 200x400
    portrait.save(tmp_path / "rot.jpg", exif=exif)

    res = normalizer.normalize_image(
        tmp_path / "rot.jpg", tmp_path / "out.jpg", max_width=400
    )
    # Orientation 6 means the display image is landscape (wider than tall).
    assert res.width >= res.height


def test_normalize_strips_exif_pii_from_output(tmp_path):
    # Standing PII invariant (plan Unit 1): a source carrying EXIF -- including a
    # GPS IFD -- must yield an output JPEG with NO EXIF/GPS. A guarantee, not an
    # accident of convert("RGB"); pin it so a future exif= on save cannot silently
    # re-leak coordinates.
    img = Image.new("RGB", (1200, 800), (123, 50, 50))
    exif = img.getexif()
    exif[0x0110] = "SecretCam"  # Model -- a plain EXIF tag
    gps = exif.get_ifd(0x8825)  # GPS IFD
    gps[1] = "N"  # GPSLatitudeRef (ASCII; rational GPSLatitude omitted -- Pillow's
    # rational writer is finicky and the Model tag already proves EXIF is present)
    src = tmp_path / "geo.jpg"
    img.save(src, format="JPEG", exif=exif)
    assert len(Image.open(src).getexif()) > 0  # source really carries EXIF

    dst = tmp_path / "out.jpg"
    normalizer.normalize_image(src, dst, max_width=800)
    out_exif = Image.open(dst).getexif()
    assert len(out_exif) == 0  # no EXIF survives on the output
    assert 0x8825 not in out_exif  # specifically no GPS IFD


# --- decompression bomb -------------------------------------------------------


def test_decompression_bomb_caught_not_crash(tmp_path, monkeypatch):
    # Lower the cap so a modest test image trips the bomb guard deterministically
    # without allocating gigabytes.
    monkeypatch.setattr(normalizer.Image, "MAX_IMAGE_PIXELS", 1000)
    src = _save_rgb(tmp_path / "bomb.png", (200, 200))  # 40k px > 1000 cap
    with pytest.raises(InputValidationError) as exc:
        normalizer.normalize_image(src, tmp_path / "out.jpg")
    assert exc.value.exit_code == 2
    assert "bomb" in str(exc.value).lower()


def test_corrupt_file_reported_as_validation_error(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not an image at all")
    with pytest.raises(InputValidationError):
        normalizer.normalize_image(bad, tmp_path / "out.jpg")


# --- working-pixel cap for the analysis path (Unit 13) ------------------------


def test_downscale_leaves_within_budget_image_untouched():
    # A normal cover (1300x640 ~= 0.83 MP) is well under the 4 MP working cap,
    # so thumbnail() must not resample it -> verdict-preserving no-op.
    img = Image.new("RGB", (1300, 640), (120, 120, 120))
    out = normalizer.downscale_to_working_pixels(img)
    assert out is img
    assert out.size == (1300, 640)


def test_downscale_bounds_oversize_image_to_working_cap():
    # An image far above the working cap is shrunk to <= the cap, aspect kept.
    cap = 1_000_000
    img = Image.new("RGB", (4000, 2000))  # 8 MP > 1 MP cap
    out = normalizer.downscale_to_working_pixels(img, max_pixels=cap)
    w, h = out.size
    assert w * h <= cap
    assert abs((w / h) - 2.0) < 0.05  # aspect ratio preserved


# --- cover composition --------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_cover_exact_dimensions_no_distortion(tmp_path, n):
    srcs = []
    for i in range(n):
        # varied aspect ratios to prove fit() crops rather than squashes
        srcs.append(_save_rgb(tmp_path / f"s{i}.jpg", (300 + i * 200, 200 + i * 50)))
    dst = tmp_path / "cover.jpg"
    out = normalizer.make_cover(srcs, dst, cover_width=1300, cover_height=640)
    with Image.open(out) as im:
        assert im.size == (1300, 640)
        assert im.mode == "RGB"  # no alpha for JPEG


def test_cover_ignores_sources_beyond_four(tmp_path):
    srcs = [_save_rgb(tmp_path / f"s{i}.jpg", (400, 400)) for i in range(6)]
    out = normalizer.make_cover(srcs, tmp_path / "cover.jpg")
    with Image.open(out) as im:
        assert im.size == (1300, 640)


def test_cover_requires_at_least_one_source(tmp_path):
    with pytest.raises(InputValidationError):
        normalizer.make_cover([], tmp_path / "cover.jpg")


def test_cover_fails_closed_on_mid_loop_load_failure(tmp_path):
    # Unit 15 guard: a source that fails to decode mid-compose must raise (not
    # return a partially filled canvas). _open_guarded already raises
    # InputValidationError and the save is never reached — lock that in.
    good = _save_rgb(tmp_path / "good.jpg", (400, 400))
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    dst = tmp_path / "cover.jpg"
    with pytest.raises(InputValidationError):
        normalizer.make_cover([good, bad], dst)
    assert not dst.exists()  # no partial cover written


def test_no_decompressionbomb_warning_filter_leaks(tmp_path):
    # After a normalize call the global warnings filter must be back to normal
    # (we used catch_warnings, so DecompressionBombWarning should not be an
    # error outside the guarded block).
    src = _save_rgb(tmp_path / "ok.jpg", (900, 600))
    normalizer.normalize_image(src, tmp_path / "out.jpg")
    # If the 'error' filter had leaked out of the guarded block, the warn below
    # would raise instead of being recorded. catch_warnings isolates the probe.
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        warnings.warn("probe", normalizer.Image.DecompressionBombWarning)
    assert len(recorded) == 1  # recorded, not raised -> filter did not leak
