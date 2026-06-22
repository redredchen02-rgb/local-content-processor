"""Human-facing review packet builder (Unit 8).

WHAT this produces (into data/jobs/<id>/review/, all files 0600):
  * cover.jpg          — a copy of the processed cover (best-effort; omitted if
                         the processed bundle has no cover yet).
  * title.txt          — the sanitized draft title.
  * review_message.txt — a template + the key sanitized draft fields. Source
                         links are rendered as INERT plain text via inert_link.
                         Built from the draft (NOT the linter's raw output) and
                         passed entirely through the output-side sanitizer (R41).
  * review_manifest.json — the FREEZE record: SHA-256 of the draft BODY, title,
                         and cover; submitted_at (caller-supplied timestamp);
                         review_status="pending".

THE FREEZE / STATE TRANSITION happens HERE: building the packet is the human's
`review-packet` action (人, not auto), and it drives PROCESSED -> REVIEW_PENDING
(plan transition table). The body/title/cover hashes recorded now are what
sign-off later binds to — modifying the draft body after this point is
detectable (hash mismatch). There is intentionally NO REVIEW_PENDING->PROCESSING
edge, so a frozen packet cannot be re-run in place (freeze via edge-absence).

SECURITY / HONESTY:
  * Every attacker-shapeable string is sanitized (escape_html / inert_link)
    before it touches a file — the packet is consumed by the GUI/operator and
    must not regain capability (R41 redline 3).
  * Files are 0600 in the job dir; deletion is best-effort (via JobStore). We
    make NO encryption claim (plan R42).
  * The hash chain lives in audit.jsonl; this module records a PII-free audit
    event with the high-entropy artifact hashes only (never title/body text)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.draft import Draft
from ...core.errors import InputValidationError
from ...core.state import JobState
from ..processor.sanitizer import escape_html, inert_link, sanitize_draft
from ..storage._fs import atomic_write_0600 as _write_0600
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore

REVIEW_MANIFEST_NAME = "review_manifest.json"
COVER_NAME = "cover.jpg"
TITLE_NAME = "title.txt"
MESSAGE_NAME = "review_message.txt"

EVENT_REVIEW_PACKET = "REVIEW_PACKET_BUILT"


# The body that sign-off binds to is the draft's event_body — the substantive
# article text. Title and cover are hashed separately so all three are pinned.
def _draft_body_text(draft: Draft) -> str:
    """The canonical body text whose hash sign-off binds to.

    We join the substantive sections deterministically so a change anywhere in
    the article body (intro / quick facts / event body / faq / summary / AI
    captions + subheads) changes the hash. Title and cover are hashed separately
    (the freeze covers body + title + cover, plan).

    AI structural pieces (captions / subheads — Unit 4) are net-new content; they
    are bound HERE so a post-freeze edit of an AI caption/subhead is caught by
    ``approve``'s hash check rather than slipping through unreviewed. Empty
    captions/subheads contribute nothing (filtered), so a caption-free draft
    hashes identically to before this change (backward compatible)."""
    parts: list[str] = [draft.intro, draft.event_body, draft.summary]
    parts.extend(draft.quick_facts)
    for item in draft.faq:
        parts.append(item.question)
        parts.append(item.answer)
    parts.extend(draft.subheads)
    parts.extend(s.caption for s in draft.image_sections)
    parts.extend(s.caption for s in draft.video_sections)
    # Tags are bound here so a post-freeze tag edit is caught by approve().
    parts.extend(draft.tags)
    return "\n".join(p for p in parts if p)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ReviewPacket:
    """The built packet: file paths + the freeze hashes the sign-off binds to."""

    job_id: str
    review_dir: Path
    title_path: Path
    message_path: Path
    manifest_path: Path
    cover_path: Path | None
    body_sha256: str
    title_sha256: str
    cover_sha256: str | None
    submitted_at: str


def _render_message(draft: Draft, *, source_urls: list[str]) -> str:
    """Build the human review message from the draft via the sanitizer.

    Everything here is already HTML-escaped (sanitize_draft) so the text is
    inert if rendered in the GUI; source links go through inert_link. The
    template is fixed text we control — only the interpolated draft fields are
    attacker-shapeable, and those are sanitized."""
    s = sanitize_draft(draft, source_urls=source_urls)
    lines: list[str] = []
    lines.append("=== 審核包 / REVIEW PACKET ===")
    lines.append("此為待審草稿。機器不會自動上架；上架由人工複製貼上並具結。")
    lines.append("(This is a draft awaiting review. The machine never auto-publishes.)")
    lines.append("")
    lines.append(f"標題 / Title: {s['title']}")
    if s["category"]:
        lines.append(f"分類 / Category: {s['category']}")
    if s["tags"]:
        lines.append("標籤 / Tags: " + ", ".join(s["tags"]))
    lines.append("")
    lines.append("引言 / Intro:")
    lines.append(s["intro"])
    lines.append("")
    if s["quick_facts"]:
        lines.append("一分鐘快速看懂 / Quick facts:")
        for f in s["quick_facts"]:
            lines.append(f"- {f}")
        lines.append("")
    lines.append("事件經過 / Body:")
    lines.append(s["event_body"])
    lines.append("")
    if s["faq"]:
        lines.append("FAQ:")
        for item in s["faq"]:
            lines.append(f"Q: {item['question']}")
            lines.append(f"A: {item['answer']}")
        lines.append("")
    if s["summary"]:
        lines.append("結尾 / Summary:")
        lines.append(s["summary"])
        lines.append("")
    # Source links: inert, non-clickable, never auto-fetched (R41).
    lines.append("來源連結（惰性純文字，不可點、不自動載入）/ Source links (inert):")
    if s["source_urls"]:
        for u in s["source_urls"]:
            lines.append(f"- {u}")
    else:
        lines.append("- (none recorded)")
    lines.append("")
    lines.append("提醒：簽核僅代表署名負責（attribution），非身分驗證（authentication）。")
    return "\n".join(lines)


def build_review_packet(
    *,
    job_id: str,
    draft: Draft,
    store: JobStore,
    audit: AuditLog,
    submitted_at: str,
    source_urls: list[str] | None = None,
    processed_cover: str | os.PathLike[str] | None = None,
    actor: str = "human",
) -> ReviewPacket:
    """Build the sanitized review packet and FREEZE the draft (PROCESSED ->
    REVIEW_PENDING).

    Triggered by a human (the `review-packet` command), not auto. Records the
    body/title/cover SHA-256 freeze hashes into review_manifest.json and a
    PII-free audit event carrying only the high-entropy hashes.

    `processed_cover` is an optional path to the cover produced by Stage 2; if
    given and present, it is COPIED into the review dir as cover.jpg (0600) and
    its hash is pinned. `submitted_at` is the caller-supplied timestamp (we never
    call datetime.now here, consistent with the rest of the codebase)."""
    record = store.get_job(job_id)
    if record is None:
        raise InputValidationError(f"unknown job: {job_id}")
    if record.state is not JobState.PROCESSED:
        raise InputValidationError(
            f"review packet requires a PROCESSED job; {job_id} is {record.state.value}"
        )

    job_dir = store.ensure_job_dir(job_id)
    review_dir = job_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(review_dir, 0o700)
    except OSError:
        pass

    source_urls = source_urls or []

    # --- freeze hashes (body + title + cover) ---
    body_text = _draft_body_text(draft)
    body_sha = _sha256_text(body_text)
    title_sha = _sha256_text(draft.title or "")

    # Default to the cover the Stage-2 media gate produced, if the caller did not
    # pass one explicitly. This is what wires media's processed/cover/cover.jpg
    # into the freeze (so the packet + sign-off bind a real cover hash).
    if processed_cover is None:
        default_cover = store.job_dir(job_id) / "processed" / "cover" / COVER_NAME
        if default_cover.exists():
            processed_cover = default_cover

    cover_path: Path | None = None
    cover_sha: str | None = None
    if processed_cover is not None:
        src = Path(processed_cover)
        if src.exists():
            cover_path = review_dir / COVER_NAME
            # Atomic copy: write to temp then os.replace so a crash mid-copy
            # never leaves a partial cover.jpg in the review dir.
            tmp_cover = cover_path.with_suffix(".jpg.tmp")
            try:
                shutil.copyfile(src, tmp_cover)
                try:
                    os.chmod(tmp_cover, 0o600)
                except OSError:
                    pass
                os.replace(tmp_cover, cover_path)
            finally:
                try:
                    tmp_cover.unlink(missing_ok=True)
                except OSError:
                    pass
            cover_sha = _sha256_file(cover_path)

    # --- sanitized human-facing files (R41) ---
    title_path = review_dir / TITLE_NAME
    _write_0600(title_path, escape_html(draft.title))

    message_path = review_dir / MESSAGE_NAME
    _write_0600(message_path, _render_message(draft, source_urls=source_urls))

    # --- freeze record ---
    manifest_path = review_dir / REVIEW_MANIFEST_NAME
    manifest = {
        "job_id": job_id,
        "submitted_at": submitted_at,
        "review_status": "pending",
        "freeze": {
            "body_sha256": body_sha,
            "title_sha256": title_sha,
            "cover_sha256": cover_sha,
        },
        # Inert source links recorded for the GUI to render as plain text.
        "source_links_inert": [inert_link(u) for u in source_urls],
        "encryption": False,  # honest: 0600 plaintext, no encryption (R42)
        "deletion": "best_effort",
    }
    _write_0600(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))

    # --- state transition: PROCESSED -> REVIEW_PENDING (the freeze point) ---
    store.set_state(job_id, JobState.REVIEW_PENDING, updated_at=submitted_at)

    # --- PII-free audit: high-entropy artifact hashes only ---
    audit.append(
        ts=submitted_at,
        stage="review",
        event=EVENT_REVIEW_PACKET,
        job_id=job_id,
        actor=actor,
        artifact_sha256=body_sha,
        extra={
            "title_sha256": title_sha,
            "cover_sha256": cover_sha,
            "review_status": "pending",
        },
    )

    return ReviewPacket(
        job_id=job_id,
        review_dir=review_dir,
        title_path=title_path,
        message_path=message_path,
        manifest_path=manifest_path,
        cover_path=cover_path,
        body_sha256=body_sha,
        title_sha256=title_sha,
        cover_sha256=cover_sha,
        submitted_at=submitted_at,
    )


def read_review_manifest(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """Read the freeze record for a job, or None if no packet was built."""
    path = store.job_dir(job_id) / "review" / REVIEW_MANIFEST_NAME
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def compute_body_sha256(draft: Draft) -> str:
    """Public helper: the body hash sign-off binds to (for re-verification)."""
    return _sha256_text(_draft_body_text(draft))


def compute_title_sha256(draft: Draft) -> str:
    """Public helper: the title hash sign-off binds to (for re-verification).

    Mirrors the freeze derivation EXACTLY — including the ``or ""`` — so a None
    title hashes to the same value it was frozen as (and not to a false mismatch)."""
    return _sha256_text(draft.title or "")


def compute_review_cover_sha256(store: JobStore, job_id: str) -> str | None:
    """Public helper: hash of the FROZEN review-dir cover (the copy made at packet
    build), or None if there is no cover. ``approve`` re-hashes this against the
    frozen ``cover_sha256`` so a post-freeze cover swap is detectable."""
    cover = store.job_dir(job_id) / "review" / COVER_NAME
    return _sha256_file(cover) if cover.exists() else None
