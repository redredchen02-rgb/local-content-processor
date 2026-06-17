#!/usr/bin/env python3
"""Helper: build a removal mask.png for a de-watermark sample case.

The mask is L-mode, white (255) where the engine should remove, black elsewhere.
For a fixed-box platform watermark this is a single rectangle — give the box and
this writes the mask sized to your image.

Usage:
    # mask sized to an existing image, white box at (x0,y0)-(x1,y1)
    ./.venv/bin/python spikes/dewatermark/make_mask.py \
        --like samples/a/case1/watermarked.png --box 480 360 600 400 \
        --out samples/a/case1/mask.png

    # or give explicit size instead of --like
    ./.venv/bin/python spikes/dewatermark/make_mask.py \
        --size 640 480 --box 480 360 600 400 --out mask.png

Pass --box more than once to mark several regions.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def build_mask(
    size: tuple[int, int], boxes: list[tuple[int, int, int, int]]
) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle((x0, y0, x1, y1), fill=255)
    return mask


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--like", type=Path, help="size the mask to this image")
    g.add_argument("--size", type=int, nargs=2, metavar=("W", "H"))
    p.add_argument(
        "--box", type=int, nargs=4, action="append", metavar=("X0", "Y0", "X1", "Y1"),
        required=True, help="a white rectangle to remove (repeatable)",
    )
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    if args.like is not None:
        with Image.open(args.like) as im:
            size = im.size
    else:
        size = (args.size[0], args.size[1])

    mask = build_mask(size, [tuple(b) for b in args.box])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mask.save(args.out, format="PNG")
    print(f"wrote {args.out} ({size[0]}x{size[1]}, {len(args.box)} box(es))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
