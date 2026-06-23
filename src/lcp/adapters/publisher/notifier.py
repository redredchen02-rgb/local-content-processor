"""Telegram group notification for SOP Step 10 (operator-triggered).

Sends the review packet's cover.jpg + title to a configured Telegram group
so the editorial team can approve before the operator publishes. Fire-and-forget:
failure is audited (NOTIFICATION_FAILED) and raises ExternalServiceError, but
NEVER changes job state. The job stays REVIEW_PENDING.

Security constraints:
- State guard: only REVIEW_PENDING jobs can be notified (blocks leaking a
  blocked/redline cover to Telegram before the operator completes recovery).
- Title is HTML-escaped before embedding in the caption (no parse_mode sent —
  plain text only, no Telegram markup engine runs — but escaping is a no-op
  defence if Telegram ever enables it silently).
- Cover is uploaded as multipart binary (never a URL — data/jobs/ is never served).
- Bot token never appears in the audit log (chat_id is not a secret; token is).
- dry_run=True validates config and reads cover but sends nothing (consistent
  with the LLM client dry-run semantics and the "no external mutation" guarantee)."""

from __future__ import annotations

import html
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ...core.config import NotificationConfig
from ...core.errors import DependencyError, ExternalServiceError, InputValidationError
from ...core.state import JobState
from ..storage.audit_log import AuditLog
from ..storage.job_store import JobStore

EVENT_NOTIFICATION_SENT = "NOTIFICATION_SENT"
EVENT_NOTIFICATION_FAILED = "NOTIFICATION_FAILED"

_TG_API_BASE = "https://api.telegram.org/bot"
_TIMEOUT = 30


def send_notification(
    job_id: str,
    review_dir: Path,
    title: str,
    config: NotificationConfig,
    audit: AuditLog,
    store: JobStore,
    *,
    bot_token: str,
    ts: str,
    dry_run: bool = False,
    actor: str = "operator",
) -> None:
    """Send cover + title to the configured Telegram group.

    Validates config and state, then fires the Telegram API call. Raises
    ExternalServiceError on network failure (audited, job state unchanged).
    Raises InputValidationError for bad config; DependencyError for missing token.

    `dry_run=True`: validates config and file presence, then returns without
    making any API call or writing any audit event."""
    # --- state guard ----------------------------------------------------------
    rec = store.get_job(job_id)
    if rec is None:
        raise InputValidationError(f"job not found: {job_id!r}")
    if rec.state != JobState.REVIEW_PENDING:
        raise InputValidationError(
            f"notify requires REVIEW_PENDING; job {job_id!r} is {rec.state.value!r}"
        )

    # --- config validation ----------------------------------------------------
    if not config.enabled:
        raise InputValidationError(
            "Telegram notification is disabled (notification.enabled=false in config)"
        )
    if not config.telegram_chat_id:
        raise InputValidationError(
            "notification.telegram_chat_id is empty — set it in config.yaml"
        )
    if not bot_token or not bot_token.strip():
        raise DependencyError("Telegram bot token is empty or missing")

    # --- cover file -----------------------------------------------------------
    cover_path = review_dir / "cover.jpg"
    has_photo = cover_path.is_file()

    if dry_run:
        return

    # HTML-escape title: no parse_mode is sent (plain text only), but escape
    # defensively in case Telegram ever enables markup silently for captions.
    safe_title = html.escape(title)
    caption = f"{safe_title}\n📋 {job_id}"

    try:
        if has_photo:
            _send_photo(bot_token, config.telegram_chat_id, cover_path, caption)
        else:
            _send_message(bot_token, config.telegram_chat_id, caption)
    except (urllib.error.URLError, OSError) as exc:
        error_type = type(exc).__name__
        audit.append(
            ts=ts,
            stage="notify",
            event=EVENT_NOTIFICATION_FAILED,
            job_id=job_id,
            actor=actor,
            extra={"error": error_type},
        )
        raise ExternalServiceError(
            f"Telegram notification failed ({error_type}): {exc}"
        ) from exc

    audit.append(
        ts=ts,
        stage="notify",
        event=EVENT_NOTIFICATION_SENT,
        job_id=job_id,
        actor=actor,
        extra={"chat_id": config.telegram_chat_id, "has_photo": has_photo},
    )


def _send_photo(token: str, chat_id: str, cover: Path, caption: str) -> None:
    """Multipart upload of cover.jpg + caption. Pure stdlib."""
    boundary = "----LcpBoundary"
    body = _multipart_body(
        boundary,
        fields={"chat_id": chat_id, "caption": caption},
        file_field="photo",
        file_name="cover.jpg",
        file_data=cover.read_bytes(),
    )
    url = f"{_TG_API_BASE}{token}/sendPhoto"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 — https only, fixed domain


def _send_message(token: str, chat_id: str, text: str) -> None:
    """Text-only fallback when cover.jpg is absent."""
    params = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"{_TG_API_BASE}{token}/sendMessage"
    req = urllib.request.Request(url, data=params)
    urllib.request.urlopen(req, timeout=_TIMEOUT)  # noqa: S310 — https only, fixed domain


def _multipart_body(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_data: bytes,
) -> bytes:
    """Build a minimal multipart/form-data body. Pure, no third-party deps."""
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n".encode()
        + file_data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)
