"""Pure judgement layer for the core rules.

Everything in this package is side-effect free: functions take *measured facts*
(width, height, laplacian variance, codec, fps, ...) and return structured
``Decision`` objects. No file or subprocess I/O lives here — that belongs in
``lcp.adapters``. This split keeps thresholds unit-testable without media files
(plan Unit 5: "純門檻判斷在 asset_rules，I/O 在 normalizer/ffprobe").
"""
