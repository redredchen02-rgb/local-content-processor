"""U4: the real-decodable-image media path — never exercised by any other fixture.

Every image fixture in the suite uses fake bytes (e.g. ``b"\\x89PNGdata"``) that
satisfy ingest's structural check but the media gate cannot decode, so the
real-image decode→normalize→judge path has never run end to end (the
"real-image full e2e only unit-covered" gap).

This test generates a deterministic, metadata-free, ``>=640x360`` PNG in-process
(no committed binary — sidesteps the unreviewable-binary / accidental-PII
concern), ingests it through the REAL ``LocalIngestCrawler``, runs the REAL media
gate, and asserts the gate PASSES the decoded image. It deliberately stops at the
media gate: once ``has_images`` is true the lint gate requires ``image_sections``
(a grounded CAPTION), which is a separate concern from "does a real image decode".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcp.adapters.crawler.base import SourceSpec
from lcp.adapters.crawler.ingest import LocalIngestCrawler
from lcp.adapters.processor.media_checker import media_presence, run_media_gate
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.job_store import JobStore
from lcp.core.config import MediaConfig
from lcp.core.models import AssetKind, AssetState, SourceType
from tests.support.pipeline_fakes import SOURCE, TITLE

# Pillow (the `media` extra) is required to both create and decode the image.
pytest.importorskip("PIL")

TS = "2026-06-22T00:00:00Z"


def _material_with_real_image(material: Path) -> Path:
    """A minimal material folder: text + a deterministic real PNG.

    A 40x24-cell checkerboard scaled (NEAREST) to 1280x720: comfortably above the
    media gate's 640x360 minimum (normalizes to 800x450) AND carries enough edge
    detail to clear the gate's blur/laplacian check — a solid-colour image is
    judged 'blurry' (variance 0). Deterministic, metadata-free, ~5 KB, generated
    in-process so nothing binary is committed."""
    from PIL import Image

    material.mkdir(parents=True, exist_ok=True)
    (material / "title.txt").write_text(TITLE, encoding="utf-8")
    (material / "body.txt").write_text(SOURCE, encoding="utf-8")
    cells_x, cells_y = 40, 24
    small = Image.new("L", (cells_x, cells_y))
    px = small.load()
    for y in range(cells_y):
        for x in range(cells_x):
            px[x, y] = 0 if (x + y) % 2 == 0 else 255
    small.resize((1280, 720), Image.NEAREST).convert("RGB").save(
        material / "cover.png", format="PNG"
    )
    return material


@pytest.fixture()
def store(tmp_path):
    return JobStore(base_dir=tmp_path / "data")


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(tmp_path / "data" / "audit.jsonl")


def test_ingest_records_the_real_image_ok(store, audit, tmp_path) -> None:
    material = _material_with_real_image(tmp_path / "material")
    spec = SourceSpec(
        job_id="img-001",
        source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir("img-001"),
        local_dir=material,
    )
    bundle = LocalIngestCrawler().crawl(spec)
    ok_images = [
        a for a in bundle.manifest.assets if a.kind is AssetKind.IMAGE and a.state is AssetState.OK
    ]
    assert len(ok_images) == 1


def test_real_image_passes_media_gate(store, audit, tmp_path) -> None:
    material = _material_with_real_image(tmp_path / "material")
    spec = SourceSpec(
        job_id="img-001",
        source_type=SourceType.LOCAL_DIR,
        job_dir=store.job_dir("img-001"),
        local_dir=material,
    )
    LocalIngestCrawler().crawl(spec)

    outcome = run_media_gate(
        job_id="img-001", store=store, audit=audit, ts=TS, media_config=MediaConfig()
    )

    # The gate decoded + normalized + judged a REAL image and did NOT park it.
    assert outcome.job_state is None, outcome.report
    assert outcome.report["image_count"] == 1
    assert outcome.report["status"] == "pass"
    # The persisted report is what drives has_images=True (the conditional-lint
    # signal) — proving the real decode reaches the same place a fake never could.
    has_images, _ = media_presence(store, "img-001")
    assert has_images is True
