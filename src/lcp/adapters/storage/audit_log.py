"""Append-only audit log: one JSON event per line in audit.jsonl.

Each line carries a hash-chain field: line_hash = sha256(prev_hash + canonical
JSON of the line without its own hash). Tampering with any past line breaks the
chain from that point on.

HONEST LIMITATION: this is tamper-EVIDENT, not tamper-PROOF. A local attacker
with root/write access can recompute the whole chain after editing — we cannot
prevent that locally (plan: 誠實 tamper-evident, 本地 root 不可防). The chain
makes silent edits detectable, nothing more.

PII rule: events MUST NOT contain raw identifiers (titles, source URLs,
authors, free-text). Only the job_id, stage/event codes, an actor name, and
OPTIONAL high-entropy artifact sha256 hashes are allowed. append() rejects
common raw-identifier keys defensively."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX-only: used for an exclusive append lock.
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows)
    fcntl = None  # type: ignore[assignment]

from ...core.errors import InputValidationError

GENESIS_HASH = "0" * 64

# Event-type vocabulary (extend as units land). ERASURE records a best-effort
# job deletion (plan: 刪除記 ERASURE 事件).
EVENT_ERASURE = "ERASURE"
EVENT_SIGNOFF_INVALIDATED = "SIGNOFF_INVALIDATED"
EVENT_SUPERSEDED = "SUPERSEDED"

# Keys that would smuggle PII into the audit. Rejected by append().
_PROHIBITED_KEYS = frozenset(
    {"title", "body", "text", "source_url", "url", "author", "domain",
     "review_message", "name", "email", "phone"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(obj: dict[str, Any]) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _line_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        (prev_hash + _canonical(payload)).encode("utf-8")
    ).hexdigest()


class AuditLog:
    """append-only audit.jsonl with a sha256 hash chain."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def _read_lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                out.append(json.loads(raw))
        return out

    def _last_line(self) -> str | None:
        """The last non-empty line of audit.jsonl via a bounded BACKWARD read.

        Reads only the tail (in 4 KiB blocks, expanding if the final record is
        bigger), so append() is O(1) per call instead of re-parsing the whole
        file — the previous _read_lines() made N appends O(N^2). Tolerates a
        missing trailing newline and any number of trailing blank lines. Returns
        None for an empty/whitespace-only file."""
        if not self.path.exists():
            return None
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            if pos == 0:
                return None
            block = 4096
            data = b""
            while pos > 0:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                stripped = data.rstrip(b"\r\n")
                nl = stripped.rfind(b"\n")
                if nl != -1:
                    return stripped[nl + 1:].decode("utf-8")
            stripped = data.rstrip(b"\r\n")
            return stripped.decode("utf-8") if stripped else None

    def _tail(self) -> tuple[int, str]:
        """Return (next_seq, prev_hash) by reading ONLY the last line (O(1))."""
        last = self._last_line()
        if last is None:
            return 0, GENESIS_HASH
        rec = json.loads(last)
        return rec["seq"] + 1, rec["hash"]

    def append(
        self,
        *,
        ts: str,
        stage: str,
        event: str,
        job_id: str,
        actor: str,
        artifact_sha256: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one event and return the persisted record.

        `ts` is an ISO8601 UTC string supplied by the caller — we never call
        datetime.now() here so callers stay deterministic/testable.

        artifact_sha256 must be a hex sha256 (high-entropy artifact CONTENT
        hash), never a hash of a raw low-entropy identifier."""
        extra = extra or {}
        prohibited = _PROHIBITED_KEYS & set(extra)
        if prohibited:
            raise InputValidationError(
                f"audit event may not carry PII fields: {sorted(prohibited)}"
            )
        if artifact_sha256 is not None and not _SHA256_RE.match(artifact_sha256):
            raise InputValidationError(
                "artifact_sha256 must be a lowercase hex sha256 digest"
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize concurrent appends with an OS-level exclusive lock held
        # ACROSS read-tail + write. Without it two threads/processes can read the
        # same tail (seq+prev_hash) and both write the same seq, corrupting the
        # chain (the GUI runs gates in background threads — plan CONCURRENCY).
        # The lock is taken on the append fd itself; we re-read the tail UNDER
        # the lock so the seq/prev_hash we commit to is the true latest.
        with self.path.open("a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                seq, prev_hash = self._tail()
                payload: dict[str, Any] = {
                    "seq": seq,
                    "ts": ts,
                    "stage": stage,
                    "event": event,
                    "job_id": job_id,
                    "actor": actor,
                    "prev_hash": prev_hash,
                }
                if artifact_sha256 is not None:
                    payload["artifact_sha256"] = artifact_sha256
                if extra:
                    payload["extra"] = extra
                record = dict(payload)
                record["hash"] = _line_hash(prev_hash, payload)

                f.write(_canonical(record) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return record

    def verify_chain(self) -> bool:
        """Recompute the chain; return False if any line was tampered with.

        Detects edits, reordering, and broken seq/prev_hash links. Cannot
        prevent a root-level full rewrite (see module docstring)."""
        prev_hash = GENESIS_HASH
        expected_seq = 0
        for line in self._read_lines():
            if "hash" not in line:
                return False
            stored_hash = line["hash"]
            payload = {k: v for k, v in line.items() if k != "hash"}
            if payload.get("seq") != expected_seq:
                return False
            if payload.get("prev_hash") != prev_hash:
                return False
            if _line_hash(prev_hash, payload) != stored_hash:
                return False
            prev_hash = stored_hash
            expected_seq += 1
        return True
