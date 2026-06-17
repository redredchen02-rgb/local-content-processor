"""Publisher adapters: human-facing review packet + sign-off responsibility loop.

The machine NEVER publishes (plan R26). This package only:
  * builds a SANITIZED, human-facing review packet into data/jobs/<id>/review/
    (PROCESSED -> REVIEW_PENDING, triggered by a human running `review-packet`),
  * records sign-off as ATTRIBUTION (not authentication) bound to the frozen
    draft body + title + cover hashes,
  * closes the responsibility loop: a human pastes the published URL + attests,
    moving APPROVED -> PUBLISHED_RECORDED. Until backfilled, the job is NOT done.

All attacker-shapeable strings in the packet go through the output-side
sanitizer (R41); source URLs render as inert plain text. Packet files are 0600,
in the job dir, best-effort deletion — NO encryption claim (plan R42)."""

from .review_packet import (
    REVIEW_MANIFEST_NAME,
    ReviewPacket,
    build_review_packet,
    read_review_manifest,
)
from .signoff import (
    DISCLAIMER,
    SignoffRecord,
    approve,
    backfill_published_url,
    reject,
    supersede,
)

__all__ = [
    "ReviewPacket",
    "build_review_packet",
    "read_review_manifest",
    "REVIEW_MANIFEST_NAME",
    "SignoffRecord",
    "approve",
    "reject",
    "supersede",
    "backfill_published_url",
    "DISCLAIMER",
]
