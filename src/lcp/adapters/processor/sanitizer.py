"""Output-side sanitization (R41) — shared by the review packet (Unit 8) and the
GUI (Unit 9).

THE INVARIANT (plan redline 3): attacker-shapeable strings (scraped title/text,
the LLM draft, a source URL, a review message) must NOT regain capability in any
downstream renderer. The LLM being zero-capability is not enough — the third
lethal-trifecta leg (a webview js_api bridge turning XSS into read/write/network
to the core) must be closed at the output boundary. So before any string is put
on screen / into a packet, it is escaped to an INERT form:

  * :func:`escape_html` — ``textContent``-equivalent escaping. Turns ``<``, ``>``,
    ``&``, ``"``, ``'`` into entities so the string RENDERS as the literal text
    the attacker wrote, never as executable markup. ``<script>`` becomes visible
    text, ``<img onerror=...>`` cannot fire.
  * :func:`inert_link` — render a source URL as PLAIN, non-clickable, escaped
    text. It is never an ``<a href>``, never auto-fetched. We do NOT parse or
    resolve the URL (no urllib/socket here) — it is just a string we escape.
  * :func:`sanitize_draft` — apply the above to every attacker-shapeable field of
    a :class:`Draft`, returning a plain dict the packet/GUI can render directly.

This module is PURE string transformation: no I/O, no network, no URL
resolution. It is in ``adapters`` (not ``core/rules``) because it is a *display*
concern, but it has the same no-side-effects discipline as the rule layer.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from ...core.draft import Draft


def escape_html(text: str | None) -> str:
    """Escape HTML special chars so `text` renders as inert literal text.

    Equivalent to assigning to ``element.textContent`` in the DOM: ``<script>``
    and ``<img src=x onerror=alert(1)>`` come out as visible, harmless text and
    can never execute. This is the root fix for R41 (output-side escaping) — it
    is applied regardless of what the input-side sanitizer did, because the
    threat model treats every draft/scraped string as hostile.

    Delegates to :func:`html.escape` (quote=True) — the stdlib reference
    implementation — which escapes ``&`` first then ``< > " '`` to ``&amp;
    &lt; &gt; &quot; &#x27;``, removing the hand-rolled ordering footgun.

    Returns "" for None. Pure: no I/O, no URL parsing."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def inert_link(url: str | None) -> str:
    """Render a source URL as plain, NON-clickable, escaped text.

    The URL is NEVER turned into an ``<a href>`` and is NEVER fetched/resolved —
    this module pulls in no networking library and does not parse the string as a
    URL. It is treated as ordinary attacker-shapeable text and HTML-escaped so
    that, even if it contains markup (``javascript:...``, ``<img onerror>``), it
    renders inert.

    The result is safe to drop into HTML as text content. Callers that want the
    raw (already-escaped) string for a plain-text file can use it directly."""
    return escape_html(url)


@dataclass(frozen=True)
class SanitizedMediaSection:
    asset_ref: str
    caption: str


def _sanitize_sections(sections) -> list[SanitizedMediaSection]:
    return [
        SanitizedMediaSection(
            asset_ref=escape_html(s.asset_ref or ""),
            caption=escape_html(s.caption or ""),
        )
        for s in sections
    ]


def sanitize_draft(draft: Draft, *, source_urls: list[str] | None = None) -> dict:
    """Escape every attacker-shapeable field of a Draft into an inert dict.

    Returns a plain dict (not a Draft) whose every string value is already
    HTML-escaped and whose `source_urls` are inert (non-clickable) — exactly the
    shape the review packet (Unit 8) and GUI (Unit 9) should render WITHOUT any
    further escaping (and never via innerHTML). Pure: no I/O, no URL resolution.

    Non-attacker-shapeable provenance fields (model/finish_reason/status) are
    passed through escaped too — cheap and consistent."""
    return {
        "title": escape_html(draft.title),
        "intro": escape_html(draft.intro),
        "quick_facts": [escape_html(f) for f in draft.quick_facts],
        "event_body": escape_html(draft.event_body),
        "image_sections": [
            {"asset_ref": s.asset_ref, "caption": s.caption}
            for s in _sanitize_sections(draft.image_sections)
        ],
        "video_sections": [
            {"asset_ref": s.asset_ref, "caption": s.caption}
            for s in _sanitize_sections(draft.video_sections)
        ],
        "faq": [
            {"question": escape_html(item.question), "answer": escape_html(item.answer)}
            for item in draft.faq
        ],
        "summary": escape_html(draft.summary),
        "tags": [escape_html(t) for t in draft.tags],
        "keywords": [escape_html(k) for k in draft.keywords],
        "category": escape_html(draft.category) if draft.category else None,
        "quotes": [escape_html(q.text) for q in draft.quotes],
        # Source links are rendered as inert plain text (never auto-fetched).
        "source_urls": [inert_link(u) for u in (source_urls or [])],
        # Provenance (not attacker-shapeable, escaped for consistency).
        "model": escape_html(draft.model) if draft.model else None,
        "finish_reason": escape_html(draft.finish_reason) if draft.finish_reason else None,
        "status": escape_html(draft.status.value),
        "review_reason": escape_html(draft.review_reason) if draft.review_reason else None,
    }
