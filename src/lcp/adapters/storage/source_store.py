"""SQLite store for reusable saved sources — a DELIBERATE PII-EXCEPTION table.

WHY THIS IS SEPARATE FROM job_store: the jobs table is PII-free by construction
(hashes + enum codes only). This table is the OPPOSITE: it stores the PLAINTEXT
``source_ref`` (a URL or local path) AND a free-text ``label`` the operator
types — both potentially PII. We keep plaintext here on purpose, because the
whole point is letting an operator RE-SUBMIT a previously used source: a hash
cannot be re-crawled. The separate module/file makes that exception explicit
and physically isolates it from the PII-free invariant.

OBLIGATIONS that ride with that exception (see also Unit 6 erasure + pii-
inventory.md):
- ``source_ref`` and ``label`` are BOTH treated as PII and BOTH erased together.
- Deletion is BEST-EFFORT only: a SQLite DELETE does not zero freed WAL/freelist
  pages. We do NOT claim cryptographic erasure; protection relies on OS full-disk
  encryption + 0600, exactly like job blobs.
- This store NEVER writes source_ref/label into audit.jsonl. CRUD here emits no
  audit event; erasure auditing (id only, never the plaintext) is owned by the
  Unit 6 erasure flow.

Concurrency mirrors JobStore: WAL + one fresh connection per call + busy_timeout.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...core.errors import InputValidationError
from .job_store import _chmod_db_0600

DB_NAME = "lcp.db"
_BUSY_TIMEOUT_MS = 5000

# Plaintext PII-exception table — see module docstring. Intentionally NOT in
# job_store's _SCHEMA so the PII-free jobs invariant stays visually intact.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_sources (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_sources_ref ON saved_sources(source_ref);
"""


@dataclass(frozen=True)
class SavedSource:
    id: str
    label: str
    source_ref: str
    created_at: str


def _row_to_source(row: sqlite3.Row) -> SavedSource:
    return SavedSource(
        id=row["id"],
        label=row["label"],
        source_ref=row["source_ref"],
        created_at=row["created_at"],
    )


class SourceStore:
    """CRUD over the saved_sources PII-exception table (shares lcp.db)."""

    def __init__(self, base_dir: str | os.PathLike[str] = "./data"):
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / DB_NAME
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # persistent; set once
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _chmod_db_0600(self.db_path)  # this store holds plaintext PII by design

    def add_source(
        self,
        *,
        label: str,
        source_ref: str,
        created_at: str,
        source_id: str | None = None,
    ) -> SavedSource:
        """Persist a reusable source. ``label``/``source_ref`` are stored
        verbatim (plaintext) — bridge callers sanitize on the way OUT, never
        here, so the original value can be re-submitted to the crawler.

        ``source_id`` is an opaque local id (NOT derived from the URL); a fresh
        uuid4 is minted when omitted."""
        label = label.strip()
        source_ref = source_ref.strip()
        if not source_ref:
            raise InputValidationError("source_ref must not be empty")
        if not label:
            raise InputValidationError("label must not be empty")
        sid = source_id or uuid.uuid4().hex
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO saved_sources (id, label, source_ref, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (sid, label, source_ref, created_at),
                )
            except sqlite3.IntegrityError as e:
                raise InputValidationError(f"saved source already exists: {sid}") from e
            conn.commit()
        finally:
            conn.close()
        return SavedSource(
            id=sid, label=label, source_ref=source_ref, created_at=created_at
        )

    def list_sources(self) -> list[SavedSource]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM saved_sources ORDER BY created_at, id"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_source(r) for r in rows]

    def get_source(self, source_id: str) -> SavedSource | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM saved_sources WHERE id = ?", (source_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_source(row) if row else None

    def delete_source(self, source_id: str) -> bool:
        """Best-effort delete by opaque id. Returns True if a row was removed.

        Best-effort: SQLite DELETE does not zero freed pages (see module
        docstring). No audit event is emitted here with the plaintext."""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM saved_sources WHERE id = ?", (source_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def delete_by_source_ref(self, source_ref: str) -> int:
        """Per-URL erasure entry point (Unit 6): remove every saved row whose
        plaintext source_ref matches. Returns the number of rows removed.

        Best-effort, same honesty boundary as delete_source."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM saved_sources WHERE source_ref = ?", (source_ref.strip(),)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def delete_all(self) -> int:
        """Full wipe of saved sources, for the all-data erasure path (Unit 6).
        Returns the number of rows removed. Best-effort (see module docstring)."""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM saved_sources")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
