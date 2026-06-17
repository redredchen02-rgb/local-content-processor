# Real de-watermark samples (operator-provided)

Drop your **owned/licensed** images here so `run_eval.py --samples spikes/dewatermark/samples`
scores real cases on your laptop. This decides Batch 2: **BUILD (which engine) or CUT**.

> ⚠️ Owned/licensed material ONLY. This is the de-watermark spike — using it on
> third-party/unlicensed assets is exactly what the compliance gate forbids.
> These files are git-ignored (see `.gitignore`); they never get committed.

## Layout

One folder per **case**, under its difficulty **bucket** (`a`–`e`):

```
samples/
  a/                      thin logo on smooth background
    case1/
      clean.png           the hand-cleaned reference (ground truth)
      watermarked.png     the input with the watermark present
      mask.png            L-mode, WHITE where to remove, black elsewhere
    case2/ ...
  b/   thin logo on texture
  c/   semi-transparent overlay
  d/   large/tiled/floating          (out-of-scope v1 — recorded, not gated)
  e/   over a face/subject
```

Aim for **6–10 cases per bucket** (30–50 total, stratified).

- `clean.png` = how the image SHOULD look after removal — your manual PS result.
  The harness scores each engine against this.
- `watermarked.png` = same image with the watermark still on it.
- `mask.png` = the region to remove. Build it with the helper:

```bash
./.venv/bin/python spikes/dewatermark/make_mask.py \
    --like samples/a/case1/watermarked.png --box 480 360 600 400 \
    --out samples/a/case1/mask.png
```

## Run

```bash
./.venv/bin/python spikes/dewatermark/run_eval.py --samples spikes/dewatermark/samples
./.venv/bin/python spikes/dewatermark/run_eval.py --samples spikes/dewatermark/samples --json
```

Read the per-bucket **publishable-rate vs bar**, **residual**, and **wall-clock ms**,
plus the **GO/NO-GO**. Bars: a/c ≥0.90, b/e ≥0.70, d ungated. Then plug a real
engine in `run_eval.py`'s `ENGINES` registry and compare.
