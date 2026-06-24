"""Content generator — create social media posts from gossip items.

Generates platform-specific content for:
- WeChat Moments (朋友圈)
- Xiaohongshu (小红书)
- Weibo (微博)
- Twitter/X

Includes: hook title, key points, hashtags, call-to-action."""

from __future__ import annotations

from ..models import GossipItem


def generate_post(item: GossipItem, platform: str = "weibo") -> dict:
    """Generate a social media post for the given platform.

    Returns a dict with title, body, hashtags, and call_to_action."""
    if platform == "xiaohongshu":
        return _xiaohongshu_post(item)
    elif platform == "wechat":
        return _wechat_post(item)
    elif platform == "twitter":
        return _twitter_post(item)
    else:
        return _weibo_post(item)


def _post_body(item: GossipItem, max_len: int = 150) -> str:
    """Resolve the best available body text for a social post.

    Prefers raw description (real source content) over the generated summary
    (which may be a category template like "娛樂八卦：{title}"). Falls back to
    the generated summary, then to the title."""
    if item.description and len(item.description) >= 5:
        return item.description[:max_len]
    return item.summary or item.title


def _weibo_post(item: GossipItem) -> dict:
    """Generate a Weibo-style post."""
    title = item.title
    summary = _post_body(item)

    # Generate hashtags based on category
    hashtags = _generate_hashtags(item)

    body = f"【{title}】\n\n{summary}\n\n{hashtags}"

    return {
        "platform": "weibo",
        "title": title,
        "body": body,
        "hashtags": hashtags,
        "call_to_action": "你怎么看？评论区聊聊",
    }


def _xiaohongshu_post(item: GossipItem) -> dict:
    """Generate a Xiaohongshu-style post."""
    title = item.title
    summary = _post_body(item)

    # Xiaohongshu uses more emojis and casual tone
    emoji_map = {
        "controversy": "🤯",
        "anger": "😤",
        "surprise": "😱",
        "humor": "😂",
        "sadness": "😢",
        "positive": "🥰",
        "neutral": "👀",
    }
    emoji = emoji_map.get(item.sentiment, "👀")

    body = f"{emoji} {title}\n\n{summary}\n\n💡 你觉得呢？"
    hashtags = _generate_hashtags(item) + " #吃瓜 #热搜"

    return {
        "platform": "xiaohongshu",
        "title": f"{emoji} {title}",
        "body": body,
        "hashtags": hashtags,
        "call_to_action": "点赞收藏，一起吃瓜",
    }


def _wechat_post(item: GossipItem) -> dict:
    """Generate a WeChat Moments-style post."""
    title = item.title
    summary = _post_body(item, max_len=80)

    body = f"{title}\n\n{summary}"

    return {
        "platform": "wechat",
        "title": title,
        "body": body,
        "hashtags": "",
        "call_to_action": "",
    }


def _twitter_post(item: GossipItem) -> dict:
    """Generate a Twitter-style post (280 char limit)."""
    title = item.title
    # Truncate for Twitter
    if len(title) > 100:
        title = title[:97] + "..."

    hashtags = _generate_hashtags(item, max_tags=3)
    body = f"{title}\n\n{hashtags}"

    # Ensure within 280 chars
    if len(body) > 280:
        body = body[:277] + "..."

    return {
        "platform": "twitter",
        "title": title,
        "body": body,
        "hashtags": hashtags,
        "call_to_action": "RT if you agree",
    }


def _generate_hashtags(item: GossipItem, max_tags: int = 5) -> str:
    """Generate relevant hashtags based on item category and title."""
    tags = ["#吃瓜"]

    # Category-based tags
    category_tags = {
        "sports": ["#體育", "#世界杯", "#足球"],
        "entertainment": ["#娛樂", "#八卦", "#明星"],
        "politics": ["#政治", "#國際"],
        "tech": ["#科技", "#AI"],
        "military": ["#軍事", "#國防"],
        "society": ["#社會", "#熱搜"],
    }
    tags.extend(category_tags.get(item.category, []))

    # Cross-platform tag
    if item.cross_platform_count > 3:
        tags.append("#全網熱議")
    elif item.cross_platform_count > 1:
        tags.append("#多平台")

    # Limit tags
    return " ".join(tags[:max_tags])


def enrich_generations(items: list[GossipItem]) -> list[GossipItem]:
    """No-op for now — generation is on-demand, not stored."""
    return items
