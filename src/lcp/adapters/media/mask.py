"""Removal-mask construction for de-watermark (plan Unit 8).

Masks are L-mode (255 = remove). v1 has NO auto-detection: a mask comes from a
config fixed-box or an operator-drawn box (human-in-the-loop). Large/floating/
tiled watermarks are explicitly out-of-scope v1. Pillow-only.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from lcp.core.errors import InputValidationError

Box = tuple[int, int, int, int]


def build_box_mask(size: tuple[int, int], boxes: list[Box]) -> Image.Image:
    """An L-mode mask with each (x0,y0,x1,y1) box painted white (255)."""
    if not boxes:
        raise InputValidationError("a de-watermark mask needs at least one box")
    w, h = size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    for x0, y0, x1, y1 in boxes:
        # clamp into bounds so a bad config box can never crash or escape canvas
        cx0, cy0 = max(0, min(x0, w)), max(0, min(y0, h))
        cx1, cy1 = max(0, min(x1, w)), max(0, min(y1, h))
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        draw.rectangle((cx0, cy0, cx1, cy1), fill=255)
    return mask


def write_box_mask(size: tuple[int, int], boxes: list[Box], dst: str | Path) -> str:
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    build_box_mask(size, boxes).save(out, format="PNG")
    return str(out)
