"""Shared LLM adapter primitives."""

from __future__ import annotations

import secrets


def make_delimiter() -> str:
    """An unpredictable per-call delimiter token so injected text cannot guess
    and 'close' the data region to escape into the instruction context."""
    return f"DATA_{secrets.token_hex(8)}"
