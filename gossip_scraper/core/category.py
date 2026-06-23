"""Smart categorization — auto-classify gossip items into major types.

Rule-based classifier using keyword matching. Fast, no ML dependencies.
Categories: entertainment, sports, politics, tech, society, military, international."""

from __future__ import annotations

from ..models import GossipItem

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "entertainment": (
        "电影",
        "電視",
        "电视",
        "明星",
        "艺人",
        "歌手",
        "演员",
        "演员",
        "综艺",
        "演唱会",
        "电影",
        "电视剧",
        "票房",
        "热搜",
        "八卦",
        "恋情",
        "分手",
        "结婚",
        "离婚",
        "出轨",
        "塌房",
        "翻车",
    ),
    "sports": (
        "世界杯",
        "世界盃",
        "奥运",
        "奧運",
        "NBA",
        "英超",
        "西甲",
        "意甲",
        "冠军",
        "奪冠",
        "夺冠",
        "比赛",
        "賽事",
        "赛事",
        "进球",
        "射門",
        "球員",
        "球员",
        "教练",
        "主帥",
        "主帅",
        " FIFA ",
        "CBA ",
    ),
    "politics": (
        "总统",
        "總統",
        "主席",
        "总理",
        "總理",
        "选举",
        "選舉",
        "政府",
        "国会",
        "國會",
        "法院",
        "法律",
        "政策",
        "制裁",
        "外交",
        "谈判",
        "談判",
        "峰会",
        "峰會",
    ),
    "tech": (
        "AI",
        "人工智能",
        "芯片",
        "晶片",
        "5G",
        "手机",
        "手機",
        "特斯拉",
        "特斯拉",
        "苹果",
        "蘋果",
        "华为",
        "華為",
        "比亚迪",
        "比亞迪",
        "电动车",
        "電動車",
        "机器人",
        "機器人",
    ),
    "society": (
        "高考",
        "考驗",
        "教育",
        "医疗",
        "醫療",
        "房价",
        "房價",
        "就业",
        "就業",
        "诈骗",
        "詐騙",
        "案件",
        "判決",
        "判决",
        "事故",
        "灾难",
        "災難",
        "救援",
    ),
    "military": (
        "导弹",
        "導彈",
        "軍事",
        "军事",
        "武器",
        "军队",
        "軍隊",
        "演习",
        "演習",
        "航母",
        "核武",
        "洲際",
        "洲际",
    ),
    "international": (
        "联合国",
        "聯合國",
        "北约",
        "北約",
        "欧盟",
        "歐盟",
        "G7",
        "G20",
        "APEC",
        "WTO",
        "全球化",
    ),
}


def classify(title: str) -> str:
    """Classify a title into a category using keyword matching.

    Returns the best-matching category, or 'other' if none match."""
    title_lower = title.lower()
    scores: dict[str, int] = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in title_lower)
        if hits:
            scores[cat] = hits
    if not scores:
        return "other"
    return max(scores, key=lambda k: scores[k])


def enrich_categories(items: list[GossipItem]) -> list[GossipItem]:
    """Set the category field for each item based on title keywords."""
    for it in items:
        if not it.category:
            it.category = classify(it.title)
    return items
