"""TrendLab freshness 방어 로직 테스트 — 수집/분석/점수 단계에서
오래된 뉴스가 TOP10에 섞여 들어가지 않는지 검증한다."""
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, patch

from app.trendlab import freshness
from app.trendlab.analyzer import _filter_collectible_items, _source_summary, extract_topics

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class _FixedDatetime(datetime):
    """analyzer.extract_topics()의 `datetime.now(timezone.utc)` 호출을 고정 시각으로 대체.
    실제 datetime을 상속하므로 생성자/isinstance는 정상 동작하고 now()만 고정된다."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


# ── freshness.parse_to_utc ───────────────────────────────────────────────────

def test_parse_iso8601_youtube_format():
    dt = freshness.parse_to_utc("2026-08-11T07:30:00Z")
    assert dt == datetime(2026, 8, 11, 7, 30, tzinfo=timezone.utc)


def test_parse_rfc2822_naver_format():
    dt = freshness.parse_to_utc("Tue, 11 Aug 2026 07:30:00 +0900")
    assert dt == datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc)


def test_parse_empty_or_none_returns_none():
    assert freshness.parse_to_utc("") is None
    assert freshness.parse_to_utc(None) is None
    assert freshness.parse_to_utc("   ") is None


def test_parse_garbage_returns_none():
    assert freshness.parse_to_utc("not a date") is None


# ── freshness.classify / is_collectible / multiplier ────────────────────────

def test_classify_recent6h():
    assert freshness.classify(NOW - timedelta(hours=2), NOW) == "recent6h"


def test_classify_recent24h():
    assert freshness.classify(NOW - timedelta(hours=18), NOW) == "recent24h"


def test_classify_recent48h():
    assert freshness.classify(NOW - timedelta(hours=40), NOW) == "recent48h"


def test_classify_aged7d():
    assert freshness.classify(NOW - timedelta(days=3), NOW) == "aged7d"


def test_classify_expired_over_7_days():
    assert freshness.classify(NOW - timedelta(days=10), NOW) == "expired"


def test_classify_invalid_when_none():
    assert freshness.classify(None, NOW) == "invalid"


def test_classify_future_beyond_tolerance():
    assert freshness.classify(NOW + timedelta(hours=12), NOW) == "future"


def test_classify_future_within_tolerance_is_not_future():
    # 6시간 이내의 사소한 미래 오차는 허용
    assert freshness.classify(NOW + timedelta(hours=2), NOW) != "future"


def test_is_collectible_true_for_recent_and_aged():
    assert freshness.is_collectible(NOW - timedelta(hours=1), NOW) is True
    assert freshness.is_collectible(NOW - timedelta(days=6), NOW) is True


def test_is_collectible_false_for_invalid_future_expired():
    assert freshness.is_collectible(None, NOW) is False
    assert freshness.is_collectible(NOW + timedelta(hours=12), NOW) is False
    assert freshness.is_collectible(NOW - timedelta(days=10), NOW) is False


def test_freshness_multiplier_tiers():
    assert freshness.freshness_multiplier(NOW - timedelta(hours=1), NOW) == 1.5
    assert freshness.freshness_multiplier(NOW - timedelta(hours=12), NOW) == 1.3
    assert freshness.freshness_multiplier(NOW - timedelta(hours=40), NOW) == 1.0
    assert freshness.freshness_multiplier(NOW - timedelta(days=3), NOW) == 0.5
    assert freshness.freshness_multiplier(NOW - timedelta(days=10), NOW) == 0.0
    assert freshness.freshness_multiplier(None, NOW) == 0.0


# ── analyzer._filter_collectible_items / _source_summary ────────────────────

def test_filter_collectible_items_drops_invalid_future_expired():
    items = [
        {"title": "a", "published_at": _iso(NOW - timedelta(hours=1))},   # keep
        {"title": "b", "published_at": None},                             # drop: invalid
        {"title": "c", "published_at": _iso(NOW + timedelta(hours=12))},  # drop: future
        {"title": "d", "published_at": _iso(NOW - timedelta(days=10))},   # drop: expired
        {"title": "e", "published_at": _iso(NOW - timedelta(days=3))},    # keep (aged7d)
    ]
    kept = _filter_collectible_items(items, NOW)
    assert {i["title"] for i in kept} == {"a", "e"}


def test_source_summary_counts_recent_and_old():
    items = [
        {"published_at": _iso(NOW - timedelta(hours=1))},
        {"published_at": _iso(NOW - timedelta(hours=30))},
        {"published_at": _iso(NOW - timedelta(days=3))},
    ]
    summary = _source_summary(items, NOW)
    assert summary["recent_source_count"] == 2
    assert summary["old_source_count"] == 1
    assert summary["latest_published_at"] == _iso(NOW - timedelta(hours=1))


def test_source_summary_empty_items():
    summary = _source_summary([], NOW)
    assert summary["recent_source_count"] == 0
    assert summary["old_source_count"] == 0
    assert summary["latest_published_at"] is None
    assert summary["freshness_label"] == ""


# ── extract_topics 통합 테스트 (Gemini 호출은 mock) ──────────────────────────
# 6가지 스펙 케이스를 검증한다.

def _mock_ner(event: str, category: str, example_title: str, count: int = 3):
    return {event: {"count": count, "category": category, "example_title": example_title}}


@pytest.mark.asyncio
async def test_case1_old_article_only_is_excluded():
    """Case 1: 2025년 기사만 존재 → 제외."""
    old_dt = NOW - timedelta(days=200)
    items = [
        {"source": "naver", "title": "손흥민 전북 이적설", "published_at": _iso(old_dt), "publisher": "a.com"},
        {"source": "naver", "title": "손흥민 전북 이적 검토", "published_at": _iso(old_dt), "publisher": "b.com"},
    ]
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(
        return_value=_mock_ner("손흥민 이적설", "스포츠", "손흥민 전북 이적설"),
    )):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert result == []


@pytest.mark.asyncio
async def test_case4_old_topic_with_fresh_mentions_is_candidate():
    """Case 4: 오래된 기사 + 오늘 새로운 기사 등장 → 후보."""
    old_dt = NOW - timedelta(days=200)
    fresh_dt = NOW - timedelta(hours=2)
    items = [
        {"source": "naver", "title": "OpenAI 신모델 발표", "published_at": _iso(old_dt), "publisher": "a.com"},
        {"source": "naver", "title": "OpenAI 신모델 발표 후속", "published_at": _iso(fresh_dt), "publisher": "b.com"},
        {"source": "naver", "title": "OpenAI 신모델 발표 반응", "published_at": _iso(fresh_dt), "publisher": "c.com"},
    ]
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(
        return_value=_mock_ner("OpenAI 신모델 발표", "IT", "OpenAI 신모델 발표"),
    )), patch("app.trendlab.analyzer._validate_topic", new=AsyncMock(return_value=True)):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert len(result) == 1
    assert result[0]["topic"] == "OpenAI 신모델 발표"
    assert result[0]["recent_source_count"] == 2
    assert result[0]["old_source_count"] == 0  # 200일 전 기사는 수집 단계에서 이미 제외(expired)


@pytest.mark.asyncio
async def test_case5_missing_published_at_is_excluded():
    """Case 5: published_at 없음 → 제외."""
    items = [
        {"source": "naver", "title": "테스트 이슈 발생", "published_at": None, "publisher": "a.com"},
        {"source": "naver", "title": "테스트 이슈 확산", "published_at": "", "publisher": "b.com"},
    ]
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(
        return_value=_mock_ner("테스트 이슈", "사회", "테스트 이슈 발생"),
    )):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert result == []


@pytest.mark.asyncio
async def test_case6_future_published_at_is_excluded():
    """Case 6: 미래 날짜 → 제외."""
    future_dt = NOW + timedelta(days=1)
    items = [
        {"source": "naver", "title": "미래 이슈 발생", "published_at": _iso(future_dt), "publisher": "a.com"},
        {"source": "naver", "title": "미래 이슈 확산", "published_at": _iso(future_dt), "publisher": "b.com"},
    ]
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(
        return_value=_mock_ner("미래 이슈", "사회", "미래 이슈 발생"),
    )):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert result == []
