"""Shared filesystem primitives: atomic 0600 writes.

The canonical text+atomic+0600 pattern (mkstemp/O_EXCL → fsync → chmod → replace)
lives here. Callers that previously inlined this logic now import from here.

Two contracts:
- ``atomic_write_0600(path, text)`` — text writes (config, review packet, signoff).
- ``write_0600_bytes(path, data)`` — bytes writes for the crawler subprocess
  (stdlib-only, importable under ``minimal_env``). NOT atomic (no tmp+replace)
  because the crawler writes in-place under a fresh subprocess.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_0600(path: Path, text: str) -> None:
    """Atomic 0600 write: unique temp via mkstemp (O_EXCL, already 0600),
    fsync, chmod 0600, os.replace. A crash mid-write never leaves a torn or
    world-readable file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_0600_bytes(path: Path, data: bytes) -> None:
    """In-place bytes write with 0600 perms. NOT atomic — use only where the
    caller is the sole writer (e.g. a Scrapy subprocess writing its own output).
    The startup umask already yields 0600; the chmod is belt-and-suspenders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
