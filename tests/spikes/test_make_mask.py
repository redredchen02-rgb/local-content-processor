"""Guard for the de-watermark mask helper (keeps the spike tooling from rotting)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HELPER = Path(__file__).resolve().parents[1].parent / "spikes" / "dewatermark" / "make_mask.py"


def _load():
    spec = importlib.util.spec_from_file_location("make_mask", _HELPER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_mask"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_build_mask_paints_boxes_white():
    m = _load()
    mask = m.build_mask((100, 80), [(10, 10, 20, 20)])
    assert mask.mode == "L"
    assert mask.getpixel((15, 15)) == 255  # inside box
    assert mask.getpixel((50, 50)) == 0    # outside box


def test_main_writes_mask(tmp_path):
    m = _load()
    out = tmp_path / "mask.png"
    rc = m.main(["--size", "64", "48", "--box", "0", "0", "10", "10", "--out", str(out)])
    assert rc == 0 and out.exists()
