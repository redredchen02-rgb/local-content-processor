"""Content summarization — generate concise summaries for gossip items.

Uses title + description + category to generate 1-2 sentence summaries.
No LLM dependency — uses template-based extraction for speed."""

from __future__ import annotations

import re

from ..models import GossipItem


def generate_summary(item: GossipItem) -> str:
    """Generate a concise summary for a gossip item.

    Uses title + description + category to create a 1-2 sentence summary.
    Falls back to title if no description is available."""
    if item.summary:
        return item.summary

    # If we have a description, use it as the base
    if item.description and len(item.description) > 20:
        summary = item.description[:150]
        # Clean up and ensure it ends properly
        if not summary.endswith(("。", "！", "？", ".", "!", "?")):
            summary = summary.rsplit("。", 1)[0] + "。" if "。" in summary else summary + "..."
        return summary

    # Generate from title based on category
    title = item.title
    category = item.category

    if category == "sports":
        return _sports_summary(title, item)
    elif category == "entertainment":
        return _entertainment_summary(title, item)
    elif category == "politics":
        return _politics_summary(title, item)
    elif category == "military":
        return _military_summary(title, item)
    elif category == "tech":
        return _tech_summary(title, item)
    else:
        return _general_summary(title, item)


def _sports_summary(title: str, item: GossipItem) -> str:
    platforms = ", ".join(item.merged_from[:3]) if item.merged_from else item.platform
    return f"體育熱點：{title}（來源：{platforms}，跨{item.cross_platform_count}個平台）"


def _entertainment_summary(title: str, item: GossipItem) -> str:
    return f"娛樂八卦：{title}"


def _politics_summary(title: str, item: GossipItem) -> str:
    return f"政治動態：{title}"


def _military_summary(title: str, item: GossipItem) -> str:
    return f"軍事要聞：{title}"


def _tech_summary(title: str, item: GossipItem) -> str:
    return f"科技資訊：{title}"


def _general_summary(title: str, item: GossipItem) -> str:
    if item.cross_platform_count > 3:
        return f"全網熱議：{title}（跨{item.cross_platform_count}個平台）"
    elif item.cross_platform_count > 1:
        return f"多平台關注：{title}"
    return title


def enrich_summaries(items: list[GossipItem]) -> list[GossipItem]:
    """Set the summary field for each item."""
    for it in items:
        if not it.summary:
            it.summary = generate_summary(it)
    return items
