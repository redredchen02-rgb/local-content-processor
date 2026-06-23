"""Trend dashboard — generate visual reports showing gossip patterns.

Generates Markdown/HTML reports with:
- Top items by score
- Category distribution
- Platform contribution
- Cross-platform analysis
- Sentiment distribution"""

from __future__ import annotations

import time
from pathlib import Path

from ..models import GossipItem


def generate_report(items: list[GossipItem], output_dir: Path | None = None) -> str:
    """Generate a Markdown trend report."""
    if not items:
        return "# No data available\n"

    lines = []
    lines.append(f"# Gossip Trend Report — {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"\n**Total items:** {len(items)}")
    lines.append(f"**Platforms:** {len(set(it.platform for it in items))}")
    lines.append("")

    # Top 10 by score
    lines.append("## Top 10 by Score\n")
    lines.append("| Rank | Title | Score | H | F | S | Platforms |")
    lines.append("|------|-------|-------|---|---|---|-----------|")
    for it in items[:10]:
        platforms = f"×{it.cross_platform_count}" if it.cross_platform_count > 1 else "1"
        lines.append(
            f"| {it.rank} | {it.title[:30]} | {it.score:.2f} | "
            f"{it.heat_score:.2f} | {it.freshness_score:.2f} | {it.surprise_score:.2f} | {platforms} |"
        )
    lines.append("")

    # Category distribution
    lines.append("## Category Distribution\n")
    cat_counts: dict[str, int] = {}
    for it in items:
        cat_counts[it.category] = cat_counts.get(it.category, 0) + 1
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        bar = "█" * count
        lines.append(f"- **{cat}**: {count} {bar}")
    lines.append("")

    # Platform contribution
    lines.append("## Platform Contribution\n")
    plat_counts: dict[str, int] = {}
    for it in items:
        for p in it.merged_from:
            plat_counts[p] = plat_counts.get(p, 0) + 1
    for plat, count in sorted(plat_counts.items(), key=lambda x: -x[1])[:8]:
        bar = "█" * count
        lines.append(f"- **{plat}**: {count} {bar}")
    lines.append("")

    # Cross-platform analysis
    lines.append("## Cross-Platform Analysis\n")
    cross_counts: dict[int, int] = {}
    for it in items:
        c = it.cross_platform_count
        cross_counts[c] = cross_counts.get(c, 0) + 1
    for c in sorted(cross_counts.keys()):
        bar = "█" * cross_counts[c]
        lines.append(f"- **×{c}**: {cross_counts[c]} topics {bar}")
    lines.append("")

    # Sentiment distribution
    lines.append("## Sentiment Distribution\n")
    sent_counts: dict[str, int] = {}
    for it in items:
        sent_counts[it.sentiment] = sent_counts.get(it.sentiment, 0) + 1
    for sent, count in sorted(sent_counts.items(), key=lambda x: -x[1]):
        bar = "█" * count
        lines.append(f"- **{sent}**: {count} {bar}")

    report = "\n".join(lines)

    # Save to file if output_dir specified
    if output_dir:
        output_dir.mkdir(exist_ok=True)
        filepath = output_dir / f"report_{time.strftime('%Y-%m-%d')}.md"
        filepath.write_text(report, encoding="utf-8")

    return report
