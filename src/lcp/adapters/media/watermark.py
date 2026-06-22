"""Official-watermark ADD primitive (Pillow I/O adapter).

A single shared transform reused by both body images and the cover (plan
Unit 1). It is a brand mark only — it asserts platform identity, NOT authorship
of the underlying material, and it never touches the source-provenance fields
recorded elsewhere.

Design rules (carried from the plan + Pillow gotchas):
  * Operate on an in-memory ``Image`` and RETURN a new ``Image`` — the caller
    decides whether/where to save, so a dry-run simply does not save (no
    watermarked file is ever written under dry-run).
  * Composite in RGBA (alpha needs a mode with alpha) then ``convert("RGB")``
    so the result is JPEG-writable (JPEG has no alpha channel).
  * The caller is expected to have already run ``exif_transpose`` (the normalizer
    and cover composer both do), so the mark lands in the visually-correct
    corner.
  * Logo assets are pre-sized per surface (body ~800px vs cover 1300x640); we
    still clamp a too-large logo so a misconfigured asset cannot crash or cover
    the whole image.
  * The decompression-bomb guard is inherited via ``normalizer._open_guarded``;
    we never set ``MAX_IMAGE_PIXELS = None``.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from lcp.core.config import WatermarkConfig
from lcp.core.errors import InputValidationError

from .normalizer import _open_guarded

# Surfaces the primitive knows about. Selects which pre-sized logo asset to use.
_KINDS = ("body", "cover")

# Fraction of the shorter edge a clamped/over-large logo is shrunk to fit.
_MAX_LOGO_FRACTION = 0.5


def _anchor_xy(
    position: str,
    item_w: int,
    item_h: int,
    base_w: int,
    base_h: int,
    margin: int,
) -> tuple[int, int]:
    """Top-left paste coordinate for ``item`` at a named corner/center."""
    if position == "center":
        return (base_w - item_w) // 2, (base_h - item_h) // 2
    parts = position.split("-")
    if (
        len(parts) != 2
        or parts[0] not in ("top", "bottom")
        or parts[1]
        not in (
            "left",
            "right",
        )
    ):
        raise InputValidationError(
            f"watermark position must be one of top/bottom-left/right or "
            f"'center' (got {position!r})"
        )
    vert, horiz = parts
    x = margin if horiz == "left" else base_w - item_w - margin
    y = margin if vert == "top" else base_h - item_h - margin
    # Clamp so a zero/large margin or oversized item never goes negative.
    return max(0, x), max(0, y)


def _scaled_opacity_alpha(img: Image.Image, opacity: float) -> Image.Image:
    """Return an RGBA copy with its alpha channel multiplied by ``opacity``."""
    rgba = img if img.mode == "RGBA" else img.convert("RGBA")
    scale = max(0.0, min(1.0, opacity))
    alpha = rgba.getchannel("A").point(lambda a: int(a * scale))
    rgba = rgba.copy()
    rgba.putalpha(alpha)
    return rgba


def _logo_overlay(
    config: WatermarkConfig, kind: str, base_w: int, base_h: int
) -> tuple[Image.Image, tuple[int, int]]:
    path = config.logo_cover_path if kind == "cover" else config.logo_body_path
    if not path:
        raise InputValidationError(
            f"watermark mode is 'logo' but no logo asset is configured for kind={kind!r}"
        )
    if not Path(path).exists():
        raise InputValidationError(f"watermark logo asset not found: {path}")
    logo = _open_guarded(path)
    try:
        logo = _scaled_opacity_alpha(logo, config.opacity)
        # Clamp an over-large logo so it can never blanket the base image.
        max_w = int(base_w * _MAX_LOGO_FRACTION)
        max_h = int(base_h * _MAX_LOGO_FRACTION)
        if logo.width > max_w or logo.height > max_h:
            logo.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        xy = _anchor_xy(config.position, logo.width, logo.height, base_w, base_h, config.margin)
        return logo, xy
    finally:
        # _scaled_opacity_alpha returned a copy; the opened handle can close.
        if logo is not None and getattr(logo, "fp", None) is not None:
            logo.close()


def _text_overlay(
    config: WatermarkConfig, base_w: int, base_h: int
) -> tuple[Image.Image, tuple[int, int]]:
    text = (config.text or "").strip()
    if not text:
        raise InputValidationError("watermark mode is 'text' but config.text is empty")
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    if config.font_path:
        if not Path(config.font_path).exists():
            raise InputValidationError(f"watermark font not found: {config.font_path}")
        try:
            font = ImageFont.truetype(config.font_path, config.font_size)
        except OSError as e:
            raise InputValidationError(f"cannot load watermark font {config.font_path}: {e}") from e
    else:
        font = ImageFont.load_default()

    # Measure with a scratch drawer, then render onto a transparent tile.
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = scratch.textbbox((0, 0), text, font=font)
    tw, th = int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])
    tile = Image.new("RGBA", (max(1, tw), max(1, th)), (0, 0, 0, 0))
    alpha = int(max(0.0, min(1.0, config.opacity)) * 255)
    fill = (*config.color, alpha)
    # Offset by bbox origin so glyphs with negative bearings aren't clipped.
    ImageDraw.Draw(tile).text((-int(bbox[0]), -int(bbox[1])), text, font=font, fill=fill)
    xy = _anchor_xy(config.position, tile.width, tile.height, base_w, base_h, config.margin)
    return tile, xy


def add_watermark(
    image: Image.Image, config: WatermarkConfig, *, kind: str = "body"
) -> Image.Image:
    """Composite the configured official watermark onto ``image``.

    Returns a NEW ``RGB`` image; the input is not mutated and nothing is written
    to disk (the caller saves, so dry-run writes nothing). ``kind`` selects the
    pre-sized logo asset for logo mode.
    """
    if kind not in _KINDS:
        raise InputValidationError(f"watermark kind must be one of {_KINDS} (got {kind!r})")
    base = image if image.mode == "RGBA" else image.convert("RGBA")
    base_w, base_h = base.size

    if config.mode == "logo":
        item, xy = _logo_overlay(config, kind, base_w, base_h)
    elif config.mode == "text":
        item, xy = _text_overlay(config, base_w, base_h)
    else:
        raise InputValidationError(f"watermark mode must be 'logo' or 'text' (got {config.mode!r})")

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay.paste(item, xy, item)
    out = Image.alpha_composite(base, overlay)
    return out.convert("RGB")
