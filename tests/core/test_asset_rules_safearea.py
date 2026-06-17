"""Unit 2: pure cover safe-area / border / aesthetic decisions (advisory-only)."""

from __future__ import annotations

from lcp.core.rules import asset_rules


def test_safe_box_is_10pct_inset():
    assert asset_rules.cover_safe_box(1300, 640) == (130, 64, 1170, 576)


def test_centered_full_bleed_tile_is_inside_safe():
    safe = asset_rules.cover_safe_box(1300, 640)
    # one full-bleed tile -> center (650,320) is well inside
    assert not asset_rules.tile_center_outside_safe((0, 0, 1300, 640), safe)


def test_split_tiles_are_inside_safe():
    safe = asset_rules.cover_safe_box(1300, 640)
    for rect in [(0, 0, 649, 640), (651, 0, 649, 640)]:
        assert not asset_rules.tile_center_outside_safe(rect, safe)


def test_tile_center_in_margin_is_flagged():
    safe = asset_rules.cover_safe_box(1300, 640)
    # a thin sliver hugging the right margin -> center outside safe box
    assert asset_rules.tile_center_outside_safe((1200, 0, 100, 640), safe)


def test_border_strip_detects_flat_black_bar():
    assert asset_rules.is_strip_border(5.0, 2.0)  # dark + flat
    assert not asset_rules.is_strip_border(120.0, 40.0)  # bright content
    assert not asset_rules.is_strip_border(5.0, 30.0)  # dark but high variance (noise)


def test_top_heavy_ratio():
    assert asset_rules.is_top_heavy(200.0, 100.0)  # 2.0 > 1.6
    assert not asset_rules.is_top_heavy(120.0, 100.0)  # 1.2
    assert asset_rules.is_top_heavy(50.0, 0.0)  # all energy up top


def test_busy_entropy_threshold():
    assert asset_rules.is_busy(7.9)
    assert not asset_rules.is_busy(5.0)


def test_judge_cover_aggregates_advisory_only():
    adv = asset_rules.judge_cover(
        tile_rects=[(1200, 0, 100, 640)],
        cover_w=1300,
        cover_h=640,
        border_strips={"left": (4.0, 1.0), "right": (130.0, 50.0),
                       "top": (130.0, 50.0), "bottom": (130.0, 50.0)},
        upper_energy=300.0,
        lower_energy=100.0,
        entropy=7.9,
    )
    assert any("tile 1" in g for g in adv.geometry)
    assert any("left" in g for g in adv.geometry)
    assert len(adv.aesthetic) == 2  # top-heavy + busy
    assert adv.has_any


def test_judge_cover_clean_has_no_advisories():
    adv = asset_rules.judge_cover(
        tile_rects=[(0, 0, 1300, 640)],
        cover_w=1300,
        cover_h=640,
        border_strips={s: (130.0, 50.0) for s in ("top", "bottom", "left", "right")},
        upper_energy=100.0,
        lower_energy=100.0,
        entropy=6.0,
    )
    assert not adv.has_any
