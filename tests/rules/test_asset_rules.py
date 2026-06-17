"""Pure judgement tests — zero file/subprocess I/O (plan Unit 5: "純")."""

from __future__ import annotations

from lcp.core.models import AssetState
from lcp.core.rules import asset_rules


# --- predicates ---------------------------------------------------------------


def test_is_too_small_true_when_below_min():
    assert asset_rules.is_too_small(100, 100, min_width=640, min_height=360)


def test_is_too_small_false_when_above_min():
    assert not asset_rules.is_too_small(1280, 720, min_width=640, min_height=360)


def test_is_too_small_either_axis_triggers():
    # wide enough but too short
    assert asset_rules.is_too_small(1280, 200, min_width=640, min_height=360)


def test_is_blurry_uses_threshold():
    assert asset_rules.is_blurry(10.0, threshold=100.0)
    assert not asset_rules.is_blurry(500.0, threshold=100.0)


# --- judge_image --------------------------------------------------------------


def test_judge_image_ok_when_large_and_sharp():
    d = asset_rules.judge_image(1280, 720, laplacian_variance=500.0)
    assert d.ok
    assert d.state == AssetState.OK
    assert d.reasons == []


def test_judge_image_too_small_needs_revision_not_blocked():
    d = asset_rules.judge_image(100, 100, laplacian_variance=500.0)
    assert d.state == AssetState.NEEDS_REVISION  # never BLOCKED (R14)
    assert any("too small" in r for r in d.reasons)


def test_judge_image_blurry_needs_revision():
    d = asset_rules.judge_image(1280, 720, laplacian_variance=1.0)
    assert d.state == AssetState.NEEDS_REVISION
    assert any("blurry" in r for r in d.reasons)


def test_judge_image_collects_multiple_reasons():
    d = asset_rules.judge_image(50, 50, laplacian_variance=1.0)
    assert len(d.reasons) == 2


def test_judge_image_skips_blur_when_variance_none():
    d = asset_rules.judge_image(1280, 720, laplacian_variance=None)
    assert d.ok


# --- judge_video / video_spec_ok ---------------------------------------------


def test_judge_video_ok():
    d = asset_rules.judge_video(
        codec="h264", fps=30.0, bitrate_mbps=4.0, width=1280, height=720
    )
    assert d.ok


def test_video_spec_ok_boolean_mirror():
    assert asset_rules.video_spec_ok("h264", 30.0, 4.0, 1280, 720)
    assert not asset_rules.video_spec_ok("vp9", 30.0, 4.0, 1280, 720)


def test_judge_video_wrong_codec():
    d = asset_rules.judge_video(
        codec="mpeg4", fps=30.0, bitrate_mbps=4.0, width=1280, height=720
    )
    assert d.state == AssetState.NEEDS_REVISION
    assert any("codec" in r for r in d.reasons)


def test_judge_video_low_bitrate():
    d = asset_rules.judge_video(
        codec="h264", fps=30.0, bitrate_mbps=0.3, width=1280, height=720
    )
    assert any("bitrate" in r for r in d.reasons)


def test_judge_video_fps_out_of_range():
    low = asset_rules.judge_video("h264", 5.0, 4.0, 1280, 720)
    high = asset_rules.judge_video("h264", 240.0, 4.0, 1280, 720)
    assert not low.ok and not high.ok


def test_judge_video_missing_facts_flagged():
    d = asset_rules.judge_video(
        codec=None, fps=None, bitrate_mbps=None, width=None, height=None
    )
    assert d.state == AssetState.NEEDS_REVISION
    # codec, fps, bitrate, resolution -> 4 reasons
    assert len(d.reasons) == 4


# --- judge_black_segments -----------------------------------------------------


def test_judge_black_none_is_ok():
    assert asset_rules.judge_black_segments([], duration=10.0).ok


def test_judge_black_small_fraction_ok():
    d = asset_rules.judge_black_segments([(0.0, 1.0)], duration=100.0)
    assert d.ok


def test_judge_black_large_fraction_needs_revision():
    d = asset_rules.judge_black_segments([(0.0, 8.0)], duration=10.0)
    assert d.state == AssetState.NEEDS_REVISION
    assert any("black" in r for r in d.reasons)


def test_judge_black_unknown_duration_flags():
    d = asset_rules.judge_black_segments([(0.0, 1.0)], duration=None)
    assert d.state == AssetState.NEEDS_REVISION
