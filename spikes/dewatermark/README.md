# Unit 6 spike — de-watermark accuracy + latency (Batch 2 go/no-go)

A **measurement** harness. It wires no production code; it scores a pluggable
inpaint engine over a stratified sample set and prints a decision table the team
reads to decide **BUILD (which engine) or CUT Batch 2**.

## Run

```bash
# offline, synthetic set, baseline engine
./.venv/bin/python spikes/dewatermark/run_eval.py
./.venv/bin/python spikes/dewatermark/run_eval.py --json

# the REAL go/no-go: the operator's OWN owned/licensed samples, on the laptop
./.venv/bin/python spikes/dewatermark/run_eval.py --samples /path/to/samples
```

## Real sample layout

```
samples/<bucket>/<case>/clean.png
                       /watermarked.png
                       /mask.png        # L, 255 = remove
```

Buckets: `a` thin logo/smooth · `b` thin logo/texture · `c` semi-transparent ·
`d` large/tiled (**out-of-scope v1**, recorded not gated) · `e` over face.

## What it measures

- **PSNR** (cleaned vs clean reference)
- **masked residual** — leftover watermark trace inside the mask (lower = cleaner)
- **publishable-rate** per bucket vs an acceptance **bar** (a/c ≥0.90, b/e ≥0.70,
  d ungated). On synthetic data publishable = residual ≤ threshold; on the real
  run this stands in for a **human** judgement.
- **wall-clock ms** per image — the single biggest unknown (no public CPU data).

## Honest limits

- The bundled run is **synthetic** — it validates MECHANICS only.
- The baseline `pillow_neighbourfill` is deliberately weak (cv2.inpaint-class).
  Real engines (**MI-GAN-ONNX** no-torch/offline, or **static-ghost** torch/video)
  plug in behind the `Engine` protocol, each isolated in its own env.
- CPU latency must be measured on the **operator's** machine; this harness only
  provides the ruler.
