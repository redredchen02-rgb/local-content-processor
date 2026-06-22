"""Geographic relevance scoring — detect topic region and match user region.

Topics are classified into regions based on keywords in the title.
The user can filter by region or get a relevance boost for their region."""

from __future__ import annotations

from ..models import GossipItem

# Region detection keywords
_REGION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "taiwan": ("台灣", "台湾", "台北", "高雄", "台積電", "台积电", "PTT", "民進黨", "國民黨"),
    "hk": ("香港", "港幣", "港元", "立法會", "港交所"),
    "macau": ("澳門", "澳门"),
    "mainland": ("北京", "上海", "深圳", "广州", "中國", "中国", "大陸", "大陆"),
    "japan": ("日本", "東京", "大阪", "日本隊", "日本球员"),
    "korea": ("韓國", "韩国", "首爾", "首尔"),
    "usa": ("美國", "美国", "華盛頓", "華爾街", "矽谷"),
    "europe": ("歐洲", "欧洲", "英國", "法国", "德國", "德国"),
    "middle_east": ("伊朗", "以色列", "中東", "中东", "沙烏地", "沙地"),
    "sports": ("世界盃", "世界杯", "奧運", "奥运", "NBA", "英超", "西甲"),
}


def detect_region(title: str) -> str:
    """Detect the primary region of a topic from its title.

    Returns the most specific region found, or 'global' if none match."""
    title_lower = title.lower()
    scores: dict[str, int] = {}
    for region, keywords in _REGION_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in title_lower)
        if hits:
            scores[region] = hits
    if not scores:
        return "global"
    return max(scores, key=scores.get)


def score_geo_relevance(item: GossipItem, user_region: str = "") -> float:
    """Score how relevant an item is to the user's region.

    Returns 0.0-1.0. If user_region is empty, returns 0.5 (neutral)."""
    if not user_region:
        return 0.5
    if item.region == user_region:
        return 1.0
    # Regional clusters — nearby regions get partial relevance
    clusters = {
        "taiwan": {"hk", "macau", "mainland"},
        "hk": {"taiwan", "macau", "mainland"},
        "macau": {"taiwan", "hk", "mainland"},
        "mainland": {"taiwan", "hk", "macau"},
    }
    if user_region in clusters and item.region in clusters.get(user_region, set()):
        return 0.7
    return 0.3


def enrich_regions(items: list[GossipItem]) -> list[GossipItem]:
    """Set the region field for each item based on title keywords."""
    for it in items:
        if not it.region:
            it.region = detect_region(it.title)
    return items
