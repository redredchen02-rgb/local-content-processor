"""Tests for gossip_scraper enrichers: category, geo, sentiment, summary.

Previously untested (L9 thin-test-coverage-enrichers finding). Covers:
- Happy paths (correct classification/enrichment)
- Edge cases (empty title, no match, short description)
- Batch idempotence (already-set fields are not overwritten)
"""

from __future__ import annotations

import pytest

from gossip_scraper.core.category import classify, enrich_categories
from gossip_scraper.core.geo import detect_region, enrich_regions, score_geo_relevance
from gossip_scraper.core.generator import generate_post
from gossip_scraper.core.sentiment import analyze_sentiment, enrich_sentiments, sentiment_to_score
from gossip_scraper.core.summary import enrich_summaries, generate_summary
from gossip_scraper.models import GossipItem


def _item(
    title: str,
    platform: str = "weibo",
    description: str = "",
    category: str = "",
    sentiment: str = "",
    region: str = "",
    summary: str = "",
    cross_platform_count: int = 1,
) -> GossipItem:
    return GossipItem(
        platform=platform,
        rank=1,
        title=title,
        description=description,
        category=category,
        sentiment=sentiment,
        region=region,
        summary=summary,
        cross_platform_count=cross_platform_count,
    )


# ---------------------------------------------------------------------------
# category.classify / enrich_categories
# ---------------------------------------------------------------------------


class TestClassify:
    def test_entertainment_keyword(self) -> None:
        assert classify("某明星出轨事件曝光") == "entertainment"

    def test_sports_keyword(self) -> None:
        assert classify("世界杯决赛今晚举行") == "sports"

    def test_politics_keyword(self) -> None:
        assert classify("总统选举结果公布") == "politics"

    def test_tech_keyword(self) -> None:
        assert classify("华为推出最新AI芯片") == "tech"

    def test_military_keyword(self) -> None:
        assert classify("解放軍導彈演習開始") == "military"

    def test_society_keyword(self) -> None:
        assert classify("高考成绩今日公布") == "society"

    def test_international_keyword(self) -> None:
        assert classify("联合国召开紧急会议") == "international"

    def test_no_match_returns_other(self) -> None:
        assert classify("今天天气不错") == "other"

    def test_empty_title_returns_other(self) -> None:
        assert classify("") == "other"

    def test_multi_keyword_picks_best(self) -> None:
        # 2 sports keywords vs 1 tech keyword → sports wins
        result = classify("NBA球员用5G手机发了推文")
        assert result == "sports"


class TestEnrichCategories:
    def test_sets_category_when_empty(self) -> None:
        items = [_item("某明星出轨事件")]
        result = enrich_categories(items)
        assert result[0].category == "entertainment"

    def test_skips_already_set(self) -> None:
        items = [_item("某明星出轨事件", category="sports")]
        enrich_categories(items)
        # Pre-set category must not be overwritten
        assert items[0].category == "sports"

    def test_batch(self) -> None:
        items = [
            _item("世界杯决赛"),
            _item("总统选举"),
        ]
        enrich_categories(items)
        assert items[0].category == "sports"
        assert items[1].category == "politics"


# ---------------------------------------------------------------------------
# geo.detect_region / enrich_regions / score_geo_relevance
# ---------------------------------------------------------------------------


class TestDetectRegion:
    def test_taiwan_keyword(self) -> None:
        assert detect_region("台積電宣佈擴廠計畫") == "taiwan"

    def test_hk_keyword(self) -> None:
        assert detect_region("香港恒生指数大跌") == "hk"

    def test_mainland_keyword(self) -> None:
        assert detect_region("北京今日发布新政策") == "mainland"

    def test_usa_keyword(self) -> None:
        assert detect_region("矽谷科技巨頭宣布裁員") == "usa"

    def test_no_match_returns_global(self) -> None:
        assert detect_region("今天天气不错") == "global"

    def test_case_insensitive(self) -> None:
        # PTT is a known Taiwan keyword
        assert detect_region("PTT熱議話題排行") == "taiwan"


class TestEnrichRegions:
    def test_sets_region_when_empty(self) -> None:
        items = [_item("台灣政治新聞")]
        enrich_regions(items)
        assert items[0].region == "taiwan"

    def test_skips_already_set(self) -> None:
        items = [_item("台灣政治新聞", region="usa")]
        enrich_regions(items)
        assert items[0].region == "usa"


class TestScoreGeoRelevance:
    def test_exact_match_returns_one(self) -> None:
        item = _item("台灣新聞", region="taiwan")
        assert score_geo_relevance(item, "taiwan") == 1.0

    def test_no_user_region_returns_neutral(self) -> None:
        item = _item("台灣新聞", region="taiwan")
        assert score_geo_relevance(item, "") == 0.5

    def test_nearby_cluster_returns_partial(self) -> None:
        # HK is in Taiwan's cluster
        item = _item("香港新聞", region="hk")
        score = score_geo_relevance(item, "taiwan")
        assert 0.5 < score < 1.0

    def test_unrelated_region_returns_low(self) -> None:
        item = _item("美國新聞", region="usa")
        assert score_geo_relevance(item, "taiwan") == 0.3


# ---------------------------------------------------------------------------
# sentiment.analyze_sentiment / enrich_sentiments / sentiment_to_score
# ---------------------------------------------------------------------------


class TestAnalyzeSentiment:
    def test_controversy_keyword(self) -> None:
        assert analyze_sentiment("某明星翻车引发大争议") == "controversy"

    def test_anger_keyword(self) -> None:
        assert analyze_sentiment("网友集体愤怒无法忍受") == "anger"

    def test_surprise_keyword(self) -> None:
        assert analyze_sentiment("竟然发生了这种事太意外了") == "surprise"

    def test_humor_keyword(self) -> None:
        assert analyze_sentiment("这个梗笑死我了哈哈哈") == "humor"

    def test_sadness_keyword(self) -> None:
        assert analyze_sentiment("令人惋惜的遗憾事件") == "sadness"

    def test_positive_keyword(self) -> None:
        assert analyze_sentiment("暖心正能量值得点赞") == "positive"

    def test_no_match_returns_neutral(self) -> None:
        assert analyze_sentiment("今天天气不错") == "neutral"

    def test_description_used(self) -> None:
        # Keyword in description (second arg) should also count
        result = analyze_sentiment("普通标题", "争议四起翻车现场")
        assert result == "controversy"


class TestEnrichSentiments:
    def test_sets_sentiment_when_empty(self) -> None:
        items = [_item("某明星翻车引发大争议")]
        enrich_sentiments(items)
        assert items[0].sentiment == "controversy"

    def test_skips_already_set(self) -> None:
        items = [_item("某明星翻车引发大争议", sentiment="positive")]
        enrich_sentiments(items)
        assert items[0].sentiment == "positive"


class TestSentimentToScore:
    def test_controversy_highest(self) -> None:
        assert sentiment_to_score("controversy") > sentiment_to_score("positive")

    def test_neutral_lowest(self) -> None:
        assert sentiment_to_score("neutral") <= sentiment_to_score("sadness")

    def test_unknown_returns_default(self) -> None:
        assert sentiment_to_score("unknown_category") == 0.2


# ---------------------------------------------------------------------------
# summary.generate_summary / enrich_summaries
# ---------------------------------------------------------------------------


class TestGenerateSummary:
    def test_returns_existing_summary(self) -> None:
        item = _item("某新聞", summary="已有摘要")
        assert generate_summary(item) == "已有摘要"

    def test_uses_description_when_long_enough(self) -> None:
        item = _item("某新聞", description="這是一段有意義的描述文字")
        result = generate_summary(item)
        assert "這是一段有意義的描述文字" in result

    def test_uses_short_description_above_threshold(self) -> None:
        # >= 5 chars threshold: should use description, not template
        item = _item("某新聞", description="爆料詳情")  # 4 chars — below threshold
        result_below = generate_summary(item)
        item2 = _item("某新聞", description="爆料詳情内幕")  # 6 chars — above threshold
        result_above = generate_summary(item2)
        # Above threshold should use description directly
        assert "爆料詳情内幕" in result_above
        # Below threshold falls through to template (doesn't contain description)
        assert "爆料詳情" not in result_below

    def test_falls_back_to_category_template(self) -> None:
        item = _item("世界杯决赛", category="sports")
        result = generate_summary(item)
        assert "體育熱點" in result

    def test_general_cross_platform(self) -> None:
        item = _item("某熱議話題", cross_platform_count=5)
        result = generate_summary(item)
        assert "全網熱議" in result

    def test_general_multi_platform(self) -> None:
        item = _item("某熱議話題", cross_platform_count=2)
        result = generate_summary(item)
        assert "多平台" in result

    def test_description_gets_ellipsis_when_no_period(self) -> None:
        item = _item("某新聞", description="沒有結尾句號的描述文字正文內容")
        result = generate_summary(item)
        assert result.endswith("...")


class TestEnrichSummaries:
    def test_sets_summary_when_empty(self) -> None:
        items = [_item("世界杯决赛", category="sports")]
        enrich_summaries(items)
        assert items[0].summary  # some text generated

    def test_skips_already_set(self) -> None:
        items = [_item("某新聞", summary="已有摘要")]
        enrich_summaries(items)
        assert items[0].summary == "已有摘要"


# ---------------------------------------------------------------------------
# generator.generate_post: verify description precedence fix
# ---------------------------------------------------------------------------


class TestGeneratePostSummaryPrecedence:
    def test_weibo_uses_description_over_template_summary(self) -> None:
        item = _item(
            "某明星出轨",
            description="該明星被拍到與神秘人約會，疑似出軌，目前尚未回應",
            category="entertainment",
        )
        # Pre-populate summary with the template value enrich_summaries would produce
        item.summary = "娛樂八卦：某明星出轨"
        post = generate_post(item, platform="weibo")
        # Body should use description (richer content), not the template summary
        assert "約會" in post["body"]
        assert "娛樂八卦" not in post["body"]

    def test_wechat_uses_description_over_template_summary(self) -> None:
        item = _item(
            "政治大新聞",
            description="內閣改組消息正式公布，三位重要閣員換人",
            category="politics",
        )
        item.summary = "政治動態：政治大新聞"
        post = generate_post(item, platform="wechat")
        assert "內閣改組" in post["body"]

    def test_falls_back_to_summary_when_no_description(self) -> None:
        item = _item("某娛樂新聞", category="entertainment")
        item.summary = "娛樂八卦：某娛樂新聞"
        post = generate_post(item, platform="weibo")
        assert "娛樂八卦" in post["body"]

    def test_falls_back_to_title_when_nothing_set(self) -> None:
        item = _item("最後防線標題")
        # No description, no summary
        post = generate_post(item, platform="weibo")
        assert "最後防線標題" in post["body"]
