"""Smart alerting — detect viral moments and notable gossip patterns.

Alert triggers:
- Cross-platform explosion (appearing on many platforms suddenly)
- High surprise + high heat (dramatic + popular)
- Sudden rank jump (rising fast)
- Breaking news patterns"""

from __future__ import annotations

from ..models import GossipItem

# Alert thresholds
CROSS_PLATFORM_ALERT = 4  # Alert when item appears on 4+ platforms
HEAT_SURPRISE_THRESHOLD = 0.6  # Alert when both heat and surprise are high
VELOCITY_THRESHOLD = 0.3  # Alert when rising fast


def check_alerts(items: list[GossipItem]) -> list[dict]:
    """Check all items for alert conditions.

    Returns a list of alert dicts with item info and alert type."""
    alerts = []
    for it in items:
        # Cross-platform explosion
        if it.cross_platform_count >= CROSS_PLATFORM_ALERT:
            alerts.append({
                "type": "cross_platform",
                "title": it.title,
                "platforms": it.cross_platform_count,
                "severity": "high" if it.cross_platform_count >= 6 else "medium",
                "message": f"出現在{it.cross_platform_count}個平台：{', '.join(it.merged_from[:5])}",
            })

        # High heat + high surprise (dramatic + popular)
        if it.heat_score >= 0.8 and it.surprise_score >= HEAT_SURPRISE_THRESHOLD:
            alerts.append({
                "type": "dramatic_popular",
                "title": it.title,
                "heat": it.heat_score,
                "surprise": it.surprise_score,
                "severity": "high",
                "message": f"高流量+高反差：H={it.heat_score:.2f} S={it.surprise_score:.2f}",
            })

        # Sudden rank jump
        if it.trend_velocity >= VELOCITY_THRESHOLD:
            alerts.append({
                "type": "rising_fast",
                "title": it.title,
                "velocity": it.trend_velocity,
                "severity": "medium",
                "message": f"快速上升中：速度={it.trend_velocity:.2f}",
            })

    # Sort by severity
    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda x: severity_order.get(str(x["severity"]), 2))

    return alerts


def format_alerts(alerts: list[dict]) -> str:
    """Format alerts for console output."""
    if not alerts:
        return "  無警報"

    lines = ["  ⚠️  ALERTS:"]
    for alert in alerts:
        icon = "🔴" if alert["severity"] == "high" else "🟡"
        lines.append(f"  {icon} [{alert['type']}] {alert['title'][:40]}")
        lines.append(f"     {alert['message']}")
    return "\n".join(lines)
