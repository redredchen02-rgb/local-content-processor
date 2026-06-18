"""Cover advisory measurements (Pillow I/O adapter, plan Unit 2).

The adapter MEASURES (edge strips, edge-energy split, entropy); the pure
``asset_rules.judge_cover`` JUDGES. All outcomes are ADVISORY — they never park
a job at needs_revision (text / 3rd-party-watermark detection is not feasibly
automatable here, so it is a human-preview note, not a gate). Pillow-only: no
numpy/opencv/torch.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageStat

from lcp.core.rules import asset_rules
from lcp.core.rules.asset_rules import CoverAdvisory

from .normalizer import _open_guarded, cover_cell_rects, downscale_to_working_pixels

_STRIP = 8  # px thickness of the edge strips sampled for letterbox detection
_SAFE_BOX_OUTLINE = (255, 80, 80)  # preview overlay colour


def _strip_stats(gray: Image.Image, side: str) -> tuple[float, float]:
    """(mean, stddev) luminance of an 8px strip on the named side."""
    w, h = gray.size
    if side == "top":
        box = (0, 0, w, _STRIP)
    elif side == "bottom":
        box = (0, h - _STRIP, w, h)
    elif side == "left":
        box = (0, 0, _STRIP, h)
    else:  # right
        box = (w - _STRIP, 0, w, h)
    stat = ImageStat.Stat(gray.crop(box))
    return float(stat.mean[0]), float(stat.stddev[0])


def _edge_energy_split(gray: Image.Image) -> tuple[float, float]:
    """Sum of edge magnitude in the upper vs lower half (FIND_EDGES)."""
    edges = gray.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    mid = h // 2
    upper = float(ImageStat.Stat(edges.crop((0, 0, w, mid))).sum[0])
    lower = float(ImageStat.Stat(edges.crop((0, mid, w, h))).sum[0])
    return upper, lower


def evaluate_cover(
    cover_path: str | Path, tile_count: int, *, cover_width: int, cover_height: int
) -> CoverAdvisory:
    """Measure a composed cover and return advisory-only findings."""
    img = _open_guarded(cover_path)
    try:
        # Bound in-process CPU: the bomb cap limits memory, not the cost of the
        # full-frame FIND_EDGES + entropy below. A normal cover (1300x640) is
        # already under the working cap, so this is a no-op and its verdict is
        # unchanged; only a pathological-but-legal multi-MP input is downscaled.
        img = downscale_to_working_pixels(img)
        gray = img.convert("L")
        border_strips = {s: _strip_stats(gray, s) for s in ("top", "bottom", "left", "right")}
        upper, lower = _edge_energy_split(gray)
        entropy = float(gray.entropy())
    finally:
        img.close()

    rects = cover_cell_rects(tile_count, cover_width, cover_height)
    return asset_rules.judge_cover(
        tile_rects=rects,
        cover_w=cover_width,
        cover_h=cover_height,
        border_strips=border_strips,
        upper_energy=upper,
        lower_energy=lower,
        entropy=entropy,
    )


def write_safe_area_preview(
    cover_path: str | Path,
    dst_path: str | Path,
    *,
    cover_width: int,
    cover_height: int,
    margin_frac: float = asset_rules.DEFAULT_COVER_SAFE_MARGIN_FRAC,
) -> str:
    """Write a copy of the cover with the safe-area box drawn, for human judgement.

    The box is a visual aid only — it does not alter the published cover."""
    img = _open_guarded(cover_path)
    try:
        preview = img.convert("RGB")
    finally:
        img.close()
    box = asset_rules.cover_safe_box(cover_width, cover_height, margin_frac)
    ImageDraw.Draw(preview).rectangle(box, outline=_SAFE_BOX_OUTLINE, width=3)
    out = Path(dst_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    preview.save(out, format="JPEG", quality=85, optimize=True)
    return str(out)
