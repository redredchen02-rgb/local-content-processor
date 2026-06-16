"""LLM adapter: OpenAI-compatible client + constrained-rewrite content assembler.

The LLM is zero-capability by construction (no tools / no network beyond a single
chat call / no write). See client.py and assembler.py module docstrings."""

from .assembler import assemble, build_system_prompt, build_user_message, sanitize_source
from .client import ChatResult, LlmClient

__all__ = [
    "LlmClient",
    "ChatResult",
    "assemble",
    "sanitize_source",
    "build_system_prompt",
    "build_user_message",
]
