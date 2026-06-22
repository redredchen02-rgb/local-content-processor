"""Input-side text sanitization (pure, no I/O) — the SINGLE source of truth.

WHY THIS LIVES IN core/: :func:`sanitize_source` is used by BOTH an adapter (the
LLM assembler, before the rewrite call) AND the pure rule layer (grounding +
lint, which clean the source the same way before comparing it to the draft). It
previously lived in ``adapters/llm/assembler`` and was imported UP into
``core/rules/grounding`` — a core->adapters layering violation (and a latent
core<->adapters import cycle). Moving the function down to core/ lets every layer
import it without reaching upward; the assembler re-exports it so its public name
(``lcp.adapters.llm.assembler.sanitize_source``) is unchanged.

It is deterministic defence-in-depth: it removes the INVISIBLE channels hidden
instructions hide in (zero-width / bidi / tag / private-use codepoints, control
chars), normalises to NFC, and deliberately leaves VISIBLE text intact — visible
"ignore the above" style text is neutralised by datamarking (it stays in the DATA
region), and stripping arbitrary visible text would corrupt legitimate quotes.
"""

from __future__ import annotations

import unicodedata

# Zero-width and invisible formatting codepoints commonly used to smuggle hidden
# instructions into scraped text.
_ZERO_WIDTH = {
    "​",  # zero width space
    "‌",  # zero width non-joiner
    "‍",  # zero width joiner
    "⁠",  # word joiner
    "﻿",  # zero width no-break space / BOM
    "­",  # soft hyphen
    "᠎",  # mongolian vowel separator
}

# Bidi / directional controls — can be abused to visually reorder hidden text.
_BIDI_CONTROLS = {
    "‪",
    "‫",
    "‬",
    "‭",
    "‮",
    "⁦",
    "⁧",
    "⁨",
    "⁩",
    "‎",
    "‏",
}


def _is_hidden_codepoint(ch: str) -> bool:
    """True if `ch` is an invisible / tag / private-use codepoint that has no
    legitimate place in scraped article text and is a classic injection vector."""
    if ch in _ZERO_WIDTH or ch in _BIDI_CONTROLS:
        return True
    cp = ord(ch)
    # Unicode Tags block (E0000-E007F): used for invisible "ASCII smuggling".
    if 0xE0000 <= cp <= 0xE007F:
        return True
    category = unicodedata.category(ch)
    # Co = private use, Cf = format (most are invisible). Keep \n, \t, \r.
    if category == "Co":
        return True
    if category == "Cf":
        return True
    return False


def sanitize_source(text: str) -> str:
    """Strip hidden-instruction payloads from scraped text BEFORE the LLM call.

    Removes zero-width / bidi / tag / private-use codepoints, normalises to NFC,
    and drops control chars (except newline/tab). This is deterministic
    defence-in-depth — it does not try to *understand* injections, it removes the
    invisible channels they hide in. Visible instructions like "ignore the above"
    are left intact on purpose: they are neutralised by datamarking (they stay in
    the DATA region the system prompt tells the model to treat as data), and
    removing arbitrary visible text would corrupt legitimate quotes."""
    if not text:
        return ""
    # NFC normalise first so compatibility forms collapse predictably.
    text = unicodedata.normalize("NFC", text)
    out = []
    for ch in text:
        if ch in ("\n", "\t", "\r"):
            out.append(ch)
            continue
        if _is_hidden_codepoint(ch):
            continue
        # Drop other C0/C1 control chars.
        if unicodedata.category(ch) in ("Cc",):
            continue
        out.append(ch)
    return "".join(out)
