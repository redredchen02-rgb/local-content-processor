"""Shared SQLite connection/init scaffolding for stores that share lcp.db.

Extracts the duplicated ``_connect``/``_init_db`` pattern from ``job_store`` and
``source_store``. Subclasses provide their own ``_SCHEMA`` and any post-init
hooks (e.g. ``_chmod_db_0600``). The manual-tx methods (``BEGIN IMMEDIATE``) stay
in ``job_store`` — this base only handles connection management and schema init.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_BUSY_TIMEOUT_MS = 10000


class SqliteBase:
    """Thin mixin for stores that share lcp.db with WAL + busy_timeout."""

    db_path: Path
    _SCHEMA: str

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(self._SCHEMA)
            conn.commit()
        finally:
            conn.close()
