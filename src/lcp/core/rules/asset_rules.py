"""Pure asset-quality judgement — no I/O, no exceptions for "bad media".

Adapters (``normalizer``/``ffprobe``) *measure* facts off disk, then feed them
here to decide pass / needs_revision. We return a structured :class:`Decision`
rather than raising, because a blurry/too-small/black image is a normal pipeline
outcome (-> ``NEEDS_REVISION``), not a program error. Hard crashes (corrupt file,
missing binary) are raised as errors by the adapters instead.

Threshold note (plan "Deferred to Implementation: 模糊/黑屏/dedup 門檻數值"):
the defaults below are *starting points* and MUST be calibrated against our own
corpus (Unit 1 spike). They are all parameters so callers can override from
config without touching this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from lcp.core.models import AssetState

# --- Default thresholds (calibration pending — plan Deferred) ----------------

# An image narrower/shorter than this in either axis is "too small" to publish.
DEFAULT_MIN_WIDTH = 640
DEFAULT_MIN_HEIGHT = 360

# Variance-of-Laplacian below this reads as "blurry". Scale is sensitive to the
# Laplacian kernel and to image size, so this is a placeholder to be calibrated.
DEFAULT_BLUR_VARIANCE_THRESHOLD = 100.0

# Video spec floors/ceilings (mirror MediaConfig defaults).
DEFAULT_VIDEO_CODEC = "h264"
DEFAULT_MIN_VIDEO_BITRATE_MBPS = 1.5
DEFAULT_MIN_VIDEO_FPS = 24.0
DEFAULT_MAX_VIDEO_FPS = 61.0
DEFAULT_MIN_VIDEO_WIDTH = 640
DEFAULT_MIN_VIDEO_HEIGHT = 360


@dataclass(frozen=True)
class Decision:
    """Outcome of a pure quality check.

    ``state`` is the per-asset state to record on the manifest. On a quality
    hit we default to ``NEEDS_REVISION`` (plan R14 / consistency note: *never*
    ``BLOCKED`` — that is reserved for risk redlines, handled in Unit 6).
    ``reasons`` is a list of human-readable, PII-free strings.
    """

    state: AssetState
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == AssetState.OK


def _passed() -> Decision:
    return Decision(state=AssetState.OK)


def _needs_revision(reasons: list[str]) -> Decision:
    return Decision(state=AssetState.NEEDS_REVISION, reasons=reasons)


# --- Boolean predicates (cheap, composable) ----------------------------------


def is_too_small(
    width: int,
    height: int,
    min_width: int = DEFAULT_MIN_WIDTH,
    min_height: int = DEFAULT_MIN_HEIGHT,
) -> bool:
    """True if either dimension is below the minimum publishable size."""
    return width < min_width or height < min_height


def is_blurry(
    laplacian_variance: float,
    threshold: float = DEFAULT_BLUR_VARIANCE_THRESHOLD,
) -> bool:
    """True if the variance-of-Laplacian is below ``threshold`` (low edge energy
    == out of focus). The variance is *measured* by the adapter; this is the
    pure decision step so the threshold is testable in isolation.

    A non-finite variance (NaN/inf from a degenerate image) is treated as blurry
    (fail closed): ``nan < threshold`` is False, so without this guard an
    unmeasurable image would silently pass the blur check."""
    if not math.isfinite(laplacian_variance):
        return True
    return laplacian_variance < threshold


def video_spec_ok(
    codec: str | None,
    fps: float | None,
    bitrate_mbps: float | None,
    width: int | None,
    height: int | None,
    *,
    expected_codec: str = DEFAULT_VIDEO_CODEC,
    min_bitrate_mbps: float = DEFAULT_MIN_VIDEO_BITRATE_MBPS,
    min_fps: float = DEFAULT_MIN_VIDEO_FPS,
    max_fps: float = DEFAULT_MAX_VIDEO_FPS,
    min_width: int = DEFAULT_MIN_VIDEO_WIDTH,
    min_height: int = DEFAULT_MIN_VIDEO_HEIGHT,
) -> bool:
    """Convenience boolean: True iff :func:`judge_video` returns OK."""
    return judge_video(
        codec=codec,
        fps=fps,
        bitrate_mbps=bitrate_mbps,
        width=width,
        height=height,
        expected_codec=expected_codec,
        min_bitrate_mbps=min_bitrate_mbps,
        min_fps=min_fps,
        max_fps=max_fps,
        min_width=min_width,
        min_height=min_height,
    ).ok


# --- Structured judgements (what the pipeline records) -----------------------


def judge_image(
    width: int,
    height: int,
    laplacian_variance: float | None = None,
    *,
    min_width: int = DEFAULT_MIN_WIDTH,
    min_height: int = DEFAULT_MIN_HEIGHT,
    blur_threshold: float = DEFAULT_BLUR_VARIANCE_THRESHOLD,
) -> Decision:
    """Judge an already-measured image. ``laplacian_variance=None`` skips the
    blur check (e.g. caller did not measure it)."""
    reasons: list[str] = []
    if is_too_small(width, height, min_width, min_height):
        reasons.append(f"image too small: {width}x{height} (min {min_width}x{min_height})")
    if laplacian_variance is not None and is_blurry(laplacian_variance, blur_threshold):
        reasons.append(
            f"image blurry: laplacian variance {laplacian_variance:.1f} < {blur_threshold:.1f}"
        )
    return _needs_revision(reasons) if reasons else _passed()


def judge_video(
    codec: str | None,
    fps: float | None,
    bitrate_mbps: float | None,
    width: int | None,
    height: int | None,
    *,
    expected_codec: str = DEFAULT_VIDEO_CODEC,
    min_bitrate_mbps: float = DEFAULT_MIN_VIDEO_BITRATE_MBPS,
    min_fps: float = DEFAULT_MIN_VIDEO_FPS,
    max_fps: float = DEFAULT_MAX_VIDEO_FPS,
    min_width: int = DEFAULT_MIN_VIDEO_WIDTH,
    min_height: int = DEFAULT_MIN_VIDEO_HEIGHT,
) -> Decision:
    """Judge measured video facts. Missing facts (``None``) are treated as a
    spec miss (we could not confirm the video meets the bar)."""
    reasons: list[str] = []

    if codec is None:
        reasons.append("video codec unknown")
    elif codec.lower() != expected_codec.lower():
        reasons.append(f"video codec {codec!r} != expected {expected_codec!r}")

    if fps is None:
        reasons.append("video fps unknown")
    elif fps < min_fps:
        reasons.append(f"video fps {fps:.2f} < min {min_fps:.2f}")
    elif fps > max_fps:
        reasons.append(f"video fps {fps:.2f} > max {max_fps:.2f}")

    if bitrate_mbps is None:
        reasons.append("video bitrate unknown")
    elif bitrate_mbps < min_bitrate_mbps:
        reasons.append(f"video bitrate {bitrate_mbps:.2f} Mbps < min {min_bitrate_mbps:.2f}")

    if width is None or height is None:
        reasons.append("video resolution unknown")
    elif width < min_width or height < min_height:
        reasons.append(f"video too small: {width}x{height} (min {min_width}x{min_height})")

    return _needs_revision(reasons) if reasons else _passed()


# --- Cover advisory checks (plan Unit 2) -------------------------------------
#
# These are ADVISORY, never needs_revision: geometry checks auto-WARN (they are
# deterministic arithmetic), aesthetics SOFT-suggest, and OCR-class checks
# (text / 3rd-party watermark in the cover) are not feasibly automatable on the
# Pillow-only stack — surfaced as a human-preview note, not a gate. Thresholds
# are starting points to calibrate on the operator's own sample.

DEFAULT_COVER_SAFE_MARGIN_FRAC = 0.1  # 10% -> safe box (130,64,1170,576) at 1300x640
DEFAULT_BORDER_MAX_MEAN = 24.0  # near-black strip mean (8-bit luminance)
DEFAULT_BORDER_MAX_STD = 8.0  # ...with low variance == a flat letterbox bar
DEFAULT_TOP_HEAVY_RATIO = 1.6  # upper/lower edge-energy ratio
DEFAULT_COVER_BUSY_ENTROPY = 7.4  # Shannon entropy (bits) of the luminance


def cover_safe_box(
    cover_w: int, cover_h: int, margin_frac: float = DEFAULT_COVER_SAFE_MARGIN_FRAC
) -> tuple[int, int, int, int]:
    """The safe rectangle (left, top, right, bottom) at ``margin_frac`` inset."""
    mx = int(cover_w * margin_frac)
    my = int(cover_h * margin_frac)
    return mx, my, cover_w - mx, cover_h - my


def tile_center_outside_safe(
    rect: tuple[int, int, int, int], safe_box: tuple[int, int, int, int]
) -> bool:
    """True if a tile's CENTER falls in the unsafe margin band.

    ``rect`` is ``(left, top, width, height)`` from compose placement. Using the
    center (not the full rect) keeps a centered full-bleed or split tile from
    always tripping, while a tile whose focal center lands in the crop/overlay
    margin is flagged."""
    left, top, w, h = rect
    cx, cy = left + w / 2, top + h / 2
    sl, st, sr, sb = safe_box
    return not (sl <= cx <= sr and st <= cy <= sb)


def is_strip_border(
    mean: float,
    std: float,
    *,
    max_mean: float = DEFAULT_BORDER_MAX_MEAN,
    max_std: float = DEFAULT_BORDER_MAX_STD,
) -> bool:
    """True if an edge strip reads as a flat (near-black) letterbox bar: low mean
    AND low variance. Std (not extrema) avoids false positives from JPEG noise."""
    return mean <= max_mean and std <= max_std


def is_top_heavy(
    upper_energy: float,
    lower_energy: float,
    *,
    ratio: float = DEFAULT_TOP_HEAVY_RATIO,
) -> bool:
    """True if the upper half carries disproportionately more edge energy."""
    if lower_energy <= 0:
        return upper_energy > 0
    return upper_energy / lower_energy > ratio


def is_busy(entropy: float, *, threshold: float = DEFAULT_COVER_BUSY_ENTROPY) -> bool:
    """True if luminance entropy is high enough to read as a crowded cover."""
    return entropy > threshold


@dataclass(frozen=True)
class CoverAdvisory:
    """Advisory-only cover findings. NEVER needs_revision (plan Unit 2)."""

    geometry: list[str] = field(default_factory=list)  # deterministic auto-warn
    aesthetic: list[str] = field(default_factory=list)  # soft suggestions

    @property
    def has_any(self) -> bool:
        return bool(self.geometry or self.aesthetic)


def judge_cover(
    *,
    tile_rects: list[tuple[int, int, int, int]],
    cover_w: int,
    cover_h: int,
    border_strips: dict[str, tuple[float, float]],
    upper_energy: float,
    lower_energy: float,
    entropy: float,
    margin_frac: float = DEFAULT_COVER_SAFE_MARGIN_FRAC,
    border_max_mean: float = DEFAULT_BORDER_MAX_MEAN,
    border_max_std: float = DEFAULT_BORDER_MAX_STD,
    top_heavy_ratio: float = DEFAULT_TOP_HEAVY_RATIO,
    busy_entropy: float = DEFAULT_COVER_BUSY_ENTROPY,
) -> CoverAdvisory:
    """Combine measured cover facts into advisory notes (no pass/fail)."""
    geometry: list[str] = []
    aesthetic: list[str] = []

    safe = cover_safe_box(cover_w, cover_h, margin_frac)
    for i, rect in enumerate(tile_rects):
        if tile_center_outside_safe(rect, safe):
            geometry.append(f"tile {i + 1} center falls outside the safe area")

    for side, (mean, std) in border_strips.items():
        if is_strip_border(mean, std, max_mean=border_max_mean, max_std=border_max_std):
            geometry.append(f"{side} edge looks like a black/letterbox border")

    if is_top_heavy(upper_energy, lower_energy, ratio=top_heavy_ratio):
        aesthetic.append("composition looks top-heavy (subject weighted to the top)")
    if is_busy(entropy, threshold=busy_entropy):
        aesthetic.append("cover looks busy/crowded — consider fewer elements")

    return CoverAdvisory(geometry=geometry, aesthetic=aesthetic)


def judge_black_segments(
    intervals: list[tuple[float, float]],
    duration: float | None,
    *,
    max_black_ratio: float = 0.2,
) -> Decision:
    """Given black intervals (start,end seconds) detected by ffmpeg, decide if
    the video has too much black to publish. Any black on a short clip, or
    >``max_black_ratio`` of total duration, -> needs_revision."""
    if not intervals:
        return _passed()
    total_black = sum(max(0.0, end - start) for start, end in intervals)
    if duration and duration > 0:
        ratio = total_black / duration
        if ratio > max_black_ratio:
            return _needs_revision(
                [f"video {ratio * 100:.0f}% black ({total_black:.1f}s of {duration:.1f}s)"]
            )
        return _passed()
    # Unknown duration but black detected -> flag for a human to look.
    return _needs_revision([f"black segment(s) detected: {len(intervals)}"])
