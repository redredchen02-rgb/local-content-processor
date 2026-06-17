"""Pillow image normalization (I/O adapter).

Responsibilities:
  * Decompression-bomb guard (a hostile tiny file that decodes to billions of
    pixels). We cap ``Image.MAX_IMAGE_PIXELS`` conservatively and promote the
    Pillow ``DecompressionBombWarning`` to an error, then *catch* it and surface
    it as a normal validation failure — never an OOM crash. We never set
    ``MAX_IMAGE_PIXELS = None``.
  * Body image: ``open -> exif_transpose -> thumbnail((800, huge), LANCZOS)
    -> save(JPEG, quality, optimize, progressive)``.
  * Cover: compose 1300x640 from 1-4 sources via a fixed layout table using
    ``ImageOps.fit`` (LANCZOS, centered) so each cell is filled without
    distortion. Canvas is RGB (JPEG has no alpha).
  * Blur measurement: variance-of-Laplacian, Pillow-only (no numpy/opencv).
    The measured variance is handed to ``asset_rules.is_blurry`` for the
    *decision* (kept pure, in core).

The decision of pass/needs_revision is NOT made here — adapters measure, the
core ``asset_rules`` judges (plan Unit 5 architecture rule).
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from lcp.core.config import WatermarkConfig
from lcp.core.errors import InputValidationError
from lcp.core.rules import asset_rules
from lcp.core.rules.asset_rules import Decision

# --- Decompression-bomb guard -------------------------------------------------
# ~50 MP: comfortably above any legitimate phone/DSLR shot (a 24 MP camera is
# ~6000x4000) but far below the hundreds-of-MP a bomb decodes to. Tunable; the
# point is it is a *finite* cap, never None.
SAFE_MAX_IMAGE_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = SAFE_MAX_IMAGE_PIXELS

# 3x3 discrete Laplacian (edge-detection) kernel for blur measurement.
_LAPLACIAN_KERNEL = ImageFilter.Kernel(
    size=(3, 3),
    kernel=[0, 1, 0, 1, -4, 1, 0, 1, 0],
    scale=1,
    offset=0,
)

# Cover layout tables: each entry is (left, top, width, height) in cover pixels.
# 1 = full bleed; 2 = side by side; 3 = one tall left + two stacked right;
# 4 = 2x2 grid. A 2px gutter is baked in by shrinking/offsetting cells.
_GUTTER = 2


def _cover_cells(n: int, cw: int, ch: int) -> list[tuple[int, int, int, int]]:
    g = _GUTTER
    if n <= 1:
        return [(0, 0, cw, ch)]
    if n == 2:
        half = (cw - g) // 2
        return [(0, 0, half, ch), (half + g, 0, cw - half - g, ch)]
    if n == 3:
        left_w = (cw - g) // 2
        right_w = cw - left_w - g
        top_h = (ch - g) // 2
        return [
            (0, 0, left_w, ch),
            (left_w + g, 0, right_w, top_h),
            (left_w + g, top_h + g, right_w, ch - top_h - g),
        ]
    # n >= 4 -> 2x2 grid (extra sources beyond 4 are ignored by caller).
    half_w = (cw - g) // 2
    half_h = (ch - g) // 2
    rw = cw - half_w - g
    rh = ch - half_h - g
    return [
        (0, 0, half_w, half_h),
        (half_w + g, 0, rw, half_h),
        (0, half_h + g, half_w, rh),
        (half_w + g, half_h + g, rw, rh),
    ]


@dataclass
class NormalizedImage:
    """Result of normalizing one body image."""

    out_path: str
    width: int
    height: int
    laplacian_variance: float
    decision: Decision


def measure_laplacian_variance(img: Image.Image) -> float:
    """Variance-of-Laplacian on the luminance channel. Higher == sharper.

    Pillow-only: convert to L, convolve with a Laplacian kernel, take the
    pixel-value variance via ``ImageStat``. Avoids adding numpy/opencv.

    The 1px border is cropped before measuring: Pillow's ``ImageFilter.Kernel``
    clamps out-of-bounds neighbours at the edge, which injects a spurious spike
    there (a flat image otherwise reads as non-flat). Cropping removes that
    artifact so a truly flat image measures ~0 variance.
    """
    gray = img.convert("L")
    edges = gray.filter(_LAPLACIAN_KERNEL)
    w, h = edges.size
    if w > 2 and h > 2:
        edges = edges.crop((1, 1, w - 1, h - 1))
    stat = ImageStat.Stat(edges)
    # var is per-band; L has a single band.
    return float(stat.var[0])


def _open_guarded(path: str | Path) -> Image.Image:
    """Open an image with the decompression-bomb warning promoted to an error.

    Raises :class:`InputValidationError` (exit 2) for bombs and for any file
    Pillow cannot decode, so the caller treats it as a per-asset failure rather
    than crashing the whole job.
    """
    p = Path(path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(p)
            img.load()  # force decode now, inside the warning/error guard
            return img
    except Image.DecompressionBombWarning as e:
        raise InputValidationError(
            f"decompression bomb refused: {p.name} exceeds "
            f"{SAFE_MAX_IMAGE_PIXELS} pixel cap"
        ) from e
    except Image.DecompressionBombError as e:
        raise InputValidationError(
            f"decompression bomb refused: {p.name} ({e})"
        ) from e
    except (OSError, ValueError, SyntaxError) as e:
        raise InputValidationError(f"cannot decode image {p.name}: {e}") from e


def normalize_image(
    src_path: str | Path,
    dst_path: str | Path,
    *,
    max_width: int = 800,
    quality: int = 90,
    min_width: int = asset_rules.DEFAULT_MIN_WIDTH,
    min_height: int = asset_rules.DEFAULT_MIN_HEIGHT,
    blur_threshold: float = asset_rules.DEFAULT_BLUR_VARIANCE_THRESHOLD,
    watermark: WatermarkConfig | None = None,
) -> NormalizedImage:
    """Normalize one body image to <= ``max_width`` wide, proportional JPEG.

    Pipeline: open(guarded) -> exif_transpose -> thumbnail(LANCZOS) ->
    [optional official watermark] -> save(JPEG, quality, optimize, progressive).
    Blur is measured on the CLEAN resized image (before the mark) and handed to
    ``asset_rules`` for the pass/needs_revision decision.
    """
    img = _open_guarded(src_path)
    try:
        img = ImageOps.exif_transpose(img)  # honour camera orientation first
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # thumbnail only shrinks; huge height cap keeps it width-driven.
        img.thumbnail((max_width, 10**9), Image.Resampling.LANCZOS)

        variance = measure_laplacian_variance(img)
        w, h = img.size

        if watermark is not None and watermark.enabled:
            from .watermark import add_watermark  # lazy: avoids import cycle

            img = add_watermark(img, watermark, kind="body")

        out = Path(dst_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        save_img = img if img.mode == "RGB" else img.convert("RGB")
        save_img.save(
            out, format="JPEG", quality=quality, optimize=True, progressive=True
        )
    finally:
        img.close()

    decision = asset_rules.judge_image(
        width=w,
        height=h,
        laplacian_variance=variance,
        min_width=min_width,
        min_height=min_height,
        blur_threshold=blur_threshold,
    )
    return NormalizedImage(
        out_path=str(out),
        width=w,
        height=h,
        laplacian_variance=variance,
        decision=decision,
    )


def make_cover(
    src_paths: Sequence[str | Path],
    dst_path: str | Path,
    *,
    cover_width: int = 1300,
    cover_height: int = 640,
    quality: int = 90,
    background: tuple[int, int, int] = (17, 17, 17),
    watermark: WatermarkConfig | None = None,
) -> str:
    """Compose a ``cover_width`` x ``cover_height`` JPEG cover from 1-4 sources.

    Uses a fixed layout table and ``ImageOps.fit`` (LANCZOS, centered) so each
    cell is cropped-to-fill without distortion. Sources beyond 4 are ignored;
    zero sources is a usage error. The cover is composed from CLEAN tiles and
    then, if enabled, watermarked ONCE after compose (so the mark is not
    inherited per-tile).
    """
    if not src_paths:
        raise InputValidationError("make_cover requires at least one source image")

    sources = list(src_paths)[:4]
    cells = _cover_cells(len(sources), cover_width, cover_height)

    canvas = Image.new("RGB", (cover_width, cover_height), background)
    try:
        for src, (cx, cy, cwid, chgt) in zip(sources, cells):
            img = _open_guarded(src)
            try:
                img = ImageOps.exif_transpose(img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                tile = ImageOps.fit(
                    img,
                    (cwid, chgt),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                canvas.paste(tile, (cx, cy))
            finally:
                img.close()

        if watermark is not None and watermark.enabled:
            from .watermark import add_watermark  # lazy: avoids import cycle

            marked = add_watermark(canvas, watermark, kind="cover")
            canvas.close()
            canvas = marked

        out = Path(dst_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(
            out, format="JPEG", quality=quality, optimize=True, progressive=True
        )
    finally:
        canvas.close()
    return str(out)
