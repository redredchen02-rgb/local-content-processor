"""Chinese sentiment analysis — detect emotional tone in gossip titles.

Rule-based analyzer using keyword matching. No ML dependencies.
Detects: anger, surprise, controversy, humor, sadness, positive."""

from __future__ import annotations

from ..models import GossipItem

# Sentiment keywords organized by emotion
_SENTIMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "controversy": (
        "争议",
        "爭議",
        "质疑",
        "質疑",
        "翻车",
        "翻車",
        "塌房",
        "人设崩塌",
        "人設崩塌",
        "口碑崩盘",
        "口碑崩盤",
        "翻车现场",
        "翻車現場",
        "内讧",
        "內訌",
        "撕逼",
        "互撕",
        "开撕",
        "開撕",
        "炮轰",
        "砲轟",
        "怒怼",
        "怒懟",
        "回怼",
        "回懟",
        "互呛",
        "互嗆",
    ),
    "anger": (
        "怒",
        "愤怒",
        "憤怒",
        "气炸",
        "氣炸",
        "炸了",
        "疯了",
        "瘋了",
        "忍无可忍",
        "忍無可忍",
        "太过分",
        "太過分",
        "欺人太甚",
        "暴怒",
        "震怒",
        "群怒",
        "公愤",
        "公憤",
    ),
    "surprise": (
        "竟然",
        "居然",
        "意外",
        "震惊",
        "震驚",
        "突发",
        "突發",
        "不敢信",
        "難以置信",
        "活久见",
        "活久見",
        "逆天",
        "离谱",
        "離譜",
        "反转",
        "反轉",
        "大跌眼镜",
        "大跌眼鏡",
    ),
    "humor": (
        "笑死",
        "笑翻",
        "笑cry",
        "哈哈",
        "搞笑",
        "沙雕",
        "逗比",
        "太好笑",
        "笑到",
        "笑果",
        "梗",
        "段子",
        "吐槽",
    ),
    "sadness": (
        "泪目",
        "淚目",
        "心疼",
        "难过",
        "難過",
        "伤心",
        "傷心",
        "遗憾",
        "遺憾",
        "惋惜",
        "痛心",
        "哀悼",
    ),
    "positive": (
        "点赞",
        "點讚",
        "好评",
        "好評",
        "暖心",
        "感人",
        "正能量",
        "感人至深",
        "值得",
        "点赞",
        "恭喜",
        "祝贺",
        "祝賀",
    ),
}


def analyze_sentiment(title: str, description: str = "") -> str:
    """Analyze the sentiment of a gossip title.

    Returns the dominant sentiment category, or 'neutral' if none match."""
    text = f"{title} {description}".lower()
    scores: dict[str, int] = {}
    for sentiment, keywords in _SENTIMENT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            scores[sentiment] = hits
    if not scores:
        return "neutral"
    return max(scores, key=lambda k: scores[k])


def enrich_sentiments(items: list[GossipItem]) -> list[GossipItem]:
    """Set the sentiment field for each item."""
    for it in items:
        if not it.sentiment:
            it.sentiment = analyze_sentiment(it.title, it.description)
    return items


def sentiment_to_score(sentiment: str) -> float:
    """Convert sentiment category to a numeric score for ranking.

    Controversy and anger get highest scores (most dramatic).
    Surprise and humor get medium scores.
    Positive and sadness get lower scores."""
    scores = {
        "controversy": 0.9,
        "anger": 0.8,
        "surprise": 0.7,
        "humor": 0.5,
        "sadness": 0.4,
        "positive": 0.3,
        "neutral": 0.2,
    }
    return scores.get(sentiment, 0.2)
