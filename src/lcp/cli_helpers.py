"""Shared CLI/GUI helpers extracted for dedup."""

from __future__ import annotations

import random
import re
import string
from pathlib import Path
from typing import Any
from urllib.parse import urlparse as _urlparse

from .adapters.clock import now as _now


def _completion_advisory(state: Any, *, dry_run: bool) -> str | None:
    """An operator-facing hint when a run did not reach a packet (Unit 5).

    dry-run never calls the LLM, so the copywriter sections stay empty and the
    draft cannot reach PROCESSED — say so plainly instead of a bare state."""
    from .core.state import JobState

    if dry_run and state is JobState.NEEDS_REVISION:
        return (
            "dry-run did not call the LLM, so image_sections/quick_facts/summary "
            "are empty and the draft cannot reach PROCESSED — re-run WITHOUT "
            "--dry-run (and with --ai-copy) for a complete review packet."
        )
    if state is JobState.NEEDS_REVISION:
        return (
            "draft parked for revision — see notes for the missing sections; a "
            "complete draft needs --ai-copy (and captions only for image bundles)."
        )
    return None


def _auto_job_id(url: str | None = None, directory: str | None = None) -> str:
    """Generate a job id from URL hostname (or dir name) + YYMMDD + 4-char random suffix."""
    ts = _now()
    date_part = ts[:10].replace("-", "")[2:]
    rand_part = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))

    if url is not None:
        hostname = _urlparse(url).hostname
        if not hostname:
            base = "job"
        else:
            base = re.sub(r"[^a-z0-9]+", "-", hostname.lower()).strip("-") or "job"
    elif directory is not None:
        dir_name = Path(directory).name.lower()
        base = re.sub(r"[^a-z0-9]+", "-", dir_name).strip("-") or "job"
    else:
        base = "job"

    suffix = f"-{date_part}-{rand_part}"
    return base[: 40 - len(suffix)] + suffix
