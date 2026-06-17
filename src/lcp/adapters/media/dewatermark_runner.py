"""Isolated de-watermark engine runner (plan Unit 8, Batch 2).

Runs the spike-chosen inpaint engine in a SUBPROCESS with a scrubbed env, exactly
like ``crawl_runner`` — so the heavy deps (torch/onnx/opencv) live in their OWN
environment and NEVER enter the main venv. The engine contract is minimal and
transport-agnostic:

    <engine_cmd...> --input <src> --mask <mask> --output <dst>

The engine writes the cleaned image to ``--output`` and exits 0; a non-zero exit
or a missing output is a FAILURE the caller maps to ``needs_revision`` (never a
silent partial). With NO ``engine_cmd`` configured the runner raises
``DependencyError`` (mirror missing-ffmpeg) — de-watermark is default-locked.

After a successful run we STRIP EXIF on the output (``convert("RGB")`` + re-save,
which drops EXIF/GPS) so no location/PII rides along on a published asset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from ...core.config import InpaintConfig
from ...core.errors import DependencyError, ExternalServiceError, InputValidationError
from ...runtime_hardening import minimal_env


class DewatermarkRunner:
    """Spawns the configured inpaint engine per asset, isolated + timed."""

    def __init__(
        self,
        config: InpaintConfig,
        *,
        subprocess_runner: Any = subprocess.run,
    ) -> None:
        self.config = config
        self._run = subprocess_runner  # injectable for tests

    def available(self) -> bool:
        return bool(self.config.enabled and self.config.engine_cmd)

    def remove(self, *, src: str | Path, mask: str | Path, dst: str | Path) -> str:
        """Run the engine on one asset, then strip EXIF on the output.

        Raises DependencyError if no engine is configured (default-locked);
        ExternalServiceError on timeout / non-zero exit / missing output (the
        caller maps that to needs_revision — never a silent partial)."""
        if not self.available():
            raise DependencyError(
                "de-watermark engine not configured (inpaint.enabled + "
                "inpaint.engine_cmd); install an isolated engine to enable removal"
            )
        src_p, mask_p, dst_p = Path(src), Path(mask), Path(dst)
        if not src_p.exists():
            raise InputValidationError(f"de-watermark input missing: {src_p}")
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            *self.config.engine_cmd,
            "--input", str(src_p),
            "--mask", str(mask_p),
            "--output", str(dst_p),
        ]
        try:
            proc = self._run(
                cmd,
                timeout=self.config.timeout_seconds,
                env=minimal_env(),  # scrubbed: no secrets, no main-venv leakage
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            raise ExternalServiceError(f"de-watermark engine timed out: {e}") from e

        rc = getattr(proc, "returncode", 1)
        if rc != 0:
            raise ExternalServiceError(
                f"de-watermark engine failed (rc={rc}); treat as low-confidence "
                "-> needs_revision, no partial output published"
            )
        if not dst_p.exists():
            raise ExternalServiceError(
                "de-watermark engine produced no output; needs_revision"
            )

        # EXIF/GPS strip on output: convert to RGB and re-save drops EXIF.
        with Image.open(dst_p) as img:
            img.load()
            cleaned = img.convert("RGB")
        cleaned.save(dst_p, format="JPEG", quality=92, optimize=True)
        return str(dst_p)
