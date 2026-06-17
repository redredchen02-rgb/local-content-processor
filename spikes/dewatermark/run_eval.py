#!/usr/bin/env python3
"""Unit 6 spike harness — de-watermark accuracy + LATENCY, the go/no-go for
Batch 2.

This is a MEASUREMENT harness, NOT a feature. It wires NO production code: it
runs a pluggable inpaint engine over a stratified sample set and prints a
decision table (engine x bucket x publishable-rate x residual x wall-clock).
The team reads that table to decide BUILD (which engine) or CUT Batch 2.

What it proves today (offline, zero-deps): the MECHANICS — samples load (or are
synthesized), an engine runs, and accuracy + latency are scored end to end. The
real go/no-go needs the OPERATOR'S OWN owned/licensed samples on the OPERATOR'S
laptop (CPU latency has no trustworthy public data — that is the whole point).

Stratified buckets (plan Unit 6):
  a) thin logo on smooth bg      d) large/tiled/floating  (out-of-scope v1)
  b) thin logo on texture        e) over face/subject
  c) semi-transparent overlay

Engines (pluggable behind the Engine protocol):
  * pillow_neighbourfill (DEFAULT, offline) — a cheap baseline: fill the masked
    region from a blurred copy. Weak on real watermarks (matches the plan's
    cv2.inpaint-baseline caveat) but proves the harness end to end with no deps.
  * MI-GAN-ONNX / static-ghost — REAL engines plug in here (--engine), each
    isolated in its own env; the harness scores them identically.

Run:
    ./.venv/bin/python spikes/dewatermark/run_eval.py
    ./.venv/bin/python spikes/dewatermark/run_eval.py --samples <dir> --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image, ImageDraw, ImageFilter

# Buckets and their honest acceptance bars (publishable-rate). 'd' is declared
# out-of-scope for v1 (large/tiled/floating) — recorded, never gated as a build.
BUCKETS = ("a", "b", "c", "d", "e")
BUCKET_BARS: dict[str, float | None] = {
    "a": 0.90, "b": 0.70, "c": 0.90, "d": None, "e": 0.70,
}
BUCKET_LABEL = {
    "a": "thin logo / smooth bg", "b": "thin logo / texture",
    "c": "semi-transparent overlay", "d": "large/tiled (out-of-scope v1)",
    "e": "over face/subject",
}
# Synthetic publishable threshold: a per-sample residual at/below this counts as
# "publishable" on the SYNTHETIC set (a stand-in for the human judgement that the
# real run uses). Tunable; the real gate is a human, not this number.
PUBLISHABLE_RESIDUAL = 0.06


@dataclass(frozen=True)
class Sample:
    """One labelled de-watermark case: a clean reference, the watermarked input,
    a removal mask (L, 255=remove), and its difficulty bucket."""

    bucket: str
    clean: Image.Image
    watermarked: Image.Image
    mask: Image.Image


class Engine(Protocol):
    """A de-watermark engine: take the watermarked image + mask, return a cleaned
    image. Real engines (MI-GAN-ONNX / static-ghost) satisfy this behind a
    subprocess/isolated env; the baseline below is pure Pillow."""

    name: str

    def remove(self, watermarked: Image.Image, mask: Image.Image) -> Image.Image:
        ...


# --- baseline engine (offline, pure Pillow) ----------------------------------


@dataclass
class NeighbourFillEngine:
    """Cheap baseline: composite a heavily-blurred copy into the masked region.

    This is deliberately weak (the plan flags cv2.inpaint/this class as a baseline
    only) — its job is to prove the harness runs and to anchor the bottom of the
    quality scale, NOT to ship."""

    name: str = "pillow_neighbourfill"
    radius: int = 12

    def remove(self, watermarked: Image.Image, mask: Image.Image) -> Image.Image:
        base = watermarked.convert("RGB")
        blurred = base.filter(ImageFilter.GaussianBlur(self.radius))
        out = base.copy()
        out.paste(blurred, (0, 0), mask.convert("L"))
        return out


# --- metrics (pure Python, no numpy) -----------------------------------------


def _psnr(a: Image.Image, b: Image.Image) -> float:
    """Peak signal-to-noise ratio (dB) between two RGB images. inf if identical."""
    pa = a.convert("RGB").tobytes()
    pb = b.convert("RGB").tobytes()
    n = min(len(pa), len(pb))
    if n == 0:
        return 0.0
    se = sum((pa[i] - pb[i]) ** 2 for i in range(n))
    mse = se / n
    if mse == 0:
        return float("inf")
    return 10 * math.log10((255.0 ** 2) / mse)


def _masked_residual(cleaned: Image.Image, clean: Image.Image, mask: Image.Image) -> float:
    """Mean absolute luminance error INSIDE the mask, normalised to 0..1. This is
    the residual watermark trace the engine left behind (lower == cleaner)."""
    cg = cleaned.convert("L").tobytes()
    rg = clean.convert("L").tobytes()
    mg = mask.convert("L").tobytes()
    total = 0
    count = 0
    for c, r, m in zip(cg, rg, mg):
        if m > 127:
            total += abs(c - r)
            count += 1
    if count == 0:
        return 0.0
    return (total / count) / 255.0


# --- sample loading / synthesis ----------------------------------------------


def _synthesize_sample(bucket: str, seed: int) -> Sample:
    """Build a deterministic synthetic case for a bucket (no binaries committed).

    clean = a procedural background; watermarked = clean + a semi-transparent
    box 'watermark'; mask = that box. Difficulty rises a→e via opacity/size so the
    baseline's residual visibly differs per bucket."""
    w, h = 160, 120
    clean = Image.new("RGB", (w, h))
    px = clean.load()
    # bucket-specific background: smooth gradient (a) vs high-freq texture (b/e)
    textured = bucket in ("b", "e")
    for y in range(h):
        for x in range(w):
            if textured:
                v = (x * 7 + y * 13 + seed * 5) % 256
                px[x, y] = (v, (v * 2) % 256, (v * 3) % 256)
            else:
                v = int(40 + 160 * (x / w))
                px[x, y] = (v, v, 200 - v // 2)

    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    # bucket geometry: small corner mark (a/b), wide overlay (c), huge (d), centre (e)
    boxes = {
        "a": (120, 95, 150, 112), "b": (120, 95, 150, 112),
        "c": (10, 50, 150, 80), "d": (5, 5, 155, 115),
        "e": (60, 45, 100, 80),
    }
    md.rectangle(boxes[bucket], fill=255)

    opacity = {"a": 120, "b": 120, "c": 90, "d": 140, "e": 160}[bucket]
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle(boxes[bucket], fill=(255, 255, 255, opacity))
    watermarked = Image.alpha_composite(clean.convert("RGBA"), overlay).convert("RGB")
    return Sample(bucket=bucket, clean=clean, watermarked=watermarked, mask=mask)


def synthesize_samples(per_bucket: int = 4) -> list[Sample]:
    """A stratified synthetic set (per_bucket per bucket). Deterministic."""
    out: list[Sample] = []
    for bucket in BUCKETS:
        for i in range(per_bucket):
            out.append(_synthesize_sample(bucket, seed=i + 1))
    return out


def load_samples(sample_dir: Path) -> list[Sample]:
    """Load real samples from a dir: each case is a subdir with clean.png,
    watermarked.png, mask.png and a parent folder named after its bucket
    (a/b/c/d/e). Missing dirs are skipped. This is how the OPERATOR points the
    harness at their OWN owned/licensed images."""
    out: list[Sample] = []
    for bucket in BUCKETS:
        bdir = sample_dir / bucket
        if not bdir.is_dir():
            continue
        for case in sorted(p for p in bdir.iterdir() if p.is_dir()):
            clean_p, wm_p, mask_p = case / "clean.png", case / "watermarked.png", case / "mask.png"
            if not (clean_p.exists() and wm_p.exists() and mask_p.exists()):
                continue
            out.append(Sample(
                bucket=bucket,
                clean=Image.open(clean_p).convert("RGB"),
                watermarked=Image.open(wm_p).convert("RGB"),
                mask=Image.open(mask_p).convert("L"),
            ))
    return out


# --- evaluation --------------------------------------------------------------


@dataclass
class BucketStat:
    bucket: str
    count: int = 0
    psnr_sum: float = 0.0
    residual_sum: float = 0.0
    publishable: int = 0
    latency_ms_sum: float = 0.0
    _psnr_inf: int = 0

    def add(self, psnr: float, residual: float, latency_ms: float) -> None:
        self.count += 1
        if math.isinf(psnr):
            self._psnr_inf += 1
        else:
            self.psnr_sum += psnr
        self.residual_sum += residual
        self.latency_ms_sum += latency_ms
        if residual <= PUBLISHABLE_RESIDUAL:
            self.publishable += 1

    @property
    def mean_psnr(self) -> float:
        finite = self.count - self._psnr_inf
        return self.psnr_sum / finite if finite else float("inf")

    @property
    def mean_residual(self) -> float:
        return self.residual_sum / self.count if self.count else 0.0

    @property
    def publishable_rate(self) -> float:
        return self.publishable / self.count if self.count else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return self.latency_ms_sum / self.count if self.count else 0.0

    def verdict(self) -> str:
        bar = BUCKET_BARS[self.bucket]
        if bar is None:
            return "out-of-scope v1 (not gated)"
        return "PASS" if self.publishable_rate >= bar else "BELOW BAR"

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "count": self.count,
            "mean_psnr": (None if math.isinf(self.mean_psnr) else round(self.mean_psnr, 2)),
            "mean_residual": round(self.mean_residual, 4),
            "publishable_rate": round(self.publishable_rate, 4),
            "bar": BUCKET_BARS[self.bucket],
            "mean_latency_ms": round(self.mean_latency_ms, 2),
            "verdict": self.verdict(),
        }


def evaluate(samples: list[Sample], engine: Engine) -> dict[str, BucketStat]:
    """Run the engine over every sample, scoring accuracy + wall-clock latency."""
    stats: dict[str, BucketStat] = {b: BucketStat(bucket=b) for b in BUCKETS}
    for s in samples:
        t0 = time.perf_counter()
        cleaned = engine.remove(s.watermarked, s.mask)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        psnr = _psnr(cleaned, s.clean)
        residual = _masked_residual(cleaned, s.clean, s.mask)
        stats[s.bucket].add(psnr, residual, latency_ms)
    return {b: st for b, st in stats.items() if st.count}


def build_report(samples: list[Sample], engine: Engine) -> dict[str, Any]:
    stats = evaluate(samples, engine)
    per_bucket = [stats[b].as_dict() for b in BUCKETS if b in stats]
    gated = [r for r in per_bucket if r["bar"] is not None]
    go = bool(gated) and all(r["verdict"] == "PASS" for r in gated)
    return {
        "engine": engine.name,
        "sample_count": len(samples),
        "per_bucket": per_bucket,
        "go_no_go": "GO" if go else "NO-GO/REVIEW",
        "note": "synthetic data validates MECHANICS only; real go/no-go needs the "
        "operator's owned samples on the target laptop (latency).",
    }


# --- engines registry --------------------------------------------------------

ENGINES: dict[str, Callable[[], Engine]] = {
    "pillow_neighbourfill": lambda: NeighbourFillEngine(),
}


def _print_table(report: dict[str, Any]) -> None:
    print("=" * 100)
    print(f"Unit 6 spike — de-watermark accuracy + latency  |  engine: {report['engine']}")
    print(f"  samples: {report['sample_count']}")
    print("  NOTE:", report["note"])
    print("=" * 100)
    print(f"  {'bucket':<28} {'n':>3}  {'PSNR':>7}  {'resid':>6}  {'pub%':>6}  {'bar':>5}  {'ms':>7}  verdict")
    print("-" * 100)
    for r in report["per_bucket"]:
        psnr = "inf" if r["mean_psnr"] is None else f"{r['mean_psnr']:.2f}"
        bar = "n/a" if r["bar"] is None else f"{r['bar']:.2f}"
        label = f"{r['bucket']} {BUCKET_LABEL[r['bucket']]}"
        print(f"  {label:<28} {r['count']:>3}  {psnr:>7}  {r['mean_residual']:>6.3f}  "
              f"{r['publishable_rate'] * 100:>5.0f}%  {bar:>5}  {r['mean_latency_ms']:>7.2f}  {r['verdict']}")
    print("-" * 100)
    print(f"  GO/NO-GO: {report['go_no_go']}")
    print("=" * 100)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=None,
                        help="dir of real samples (<bucket>/<case>/{clean,watermarked,mask}.png); "
                        "omitted -> a deterministic synthetic set")
    parser.add_argument("--engine", default="pillow_neighbourfill", choices=sorted(ENGINES),
                        help="inpaint engine to score")
    parser.add_argument("--per-bucket", type=int, default=4,
                        help="synthetic samples per bucket when --samples is omitted")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    args = parser.parse_args(argv)

    if args.samples is not None:
        samples = load_samples(args.samples)
        if not samples:
            print(f"error: no samples under {args.samples}", file=sys.stderr)
            return 2
    else:
        samples = synthesize_samples(args.per_bucket)

    engine = ENGINES[args.engine]()
    report = build_report(samples, engine)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
