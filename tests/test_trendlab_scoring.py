"""TrendLab BlogScore / TopicPurity / Article Eligibility / Topic Deduplication 테스트.

TrendScore(얼마나 뜨는가)와 BlogScore(블로그로 쓸 가치가 있는가)를 분리하고, 관련
source가 부족한 topic은 Trend가 아무리 높아도 Blog TOP10에서 제외되는지, 그리고
의미가 겹치는 topic(취업자 수 증가/청년 고용)이 하나로 묶이는지를 검증한다.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.trendlab import scoring
from app.trendlab.analyzer import _editorial_bonus, extract_topics
from app.trendlab.relevance import gate_sources

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _item(title: str, desc: str = "", published_at: str | None = None) -> dict:
    return {
        "source": "naver", "title": title, "description": desc,
        "published_at": published_at, "publisher": "a.com", "url": "",
    }


# ── compute_search_intent ─────────────────────────────────────────────────────

def test_search_intent_concrete_event_scores_high():
    score, _ = scoring.compute_search_intent("IFA 2026 삼성 신제품 공개")
    assert score >= 15


def test_search_intent_vague_topic_scores_low():
    score, reason = scoring.compute_search_intent("오늘의 좋은 이슈")
    assert score < 10
    assert "불명확" in reason


def test_search_intent_never_fabricates_search_volume():
    """반환값에 숫자가 있어도 그건 0~20 점수이지 검색량이 아니다 — reason에 검색량 단어가 없어야 한다."""
    _, reason = scoring.compute_search_intent("영천 청년 시민 축제")
    assert "검색량" not in reason


# ── compute_independent_source_count (source independence) ──────────────────

def test_independent_source_count_collapses_duplicate_wire_reports():
    """언론사 5곳이 사실상 같은 제목의 보도자료를 받아쓴 경우 1개로 센다."""
    sources = [_item("정부 청년 고용 대책 발표") for _ in range(5)]
    assert scoring.compute_independent_source_count(sources) == 1


def test_independent_source_count_counts_distinct_stories_separately():
    sources = [
        _item("정부 청년 고용 대책 발표"),
        _item("IFA 2026 삼성 신제품 공개 현장"),
        _item("국제유가 급등 배경 분석"),
    ]
    assert scoring.compute_independent_source_count(sources) == 3


# ── compute_freshness_score — "관련 없는 최신 뉴스"에 속지 않는다 ────────────

def test_freshness_score_uses_relevant_source_date_not_wallclock():
    recent_iso = _iso(NOW - timedelta(hours=3))
    score, _ = scoring.compute_freshness_score(recent_iso, now=NOW)
    assert score == 15


def test_freshness_score_old_relevant_source_scores_zero():
    old_iso = _iso(NOW - timedelta(days=10))
    score, _ = scoring.compute_freshness_score(old_iso, now=NOW)
    assert score == 0


def test_freshness_score_missing_date_scores_zero():
    score, reason = scoring.compute_freshness_score(None, now=NOW)
    assert score == 0
    assert "알 수 없음" in reason


# ── compute_topic_purity_score / compute_article_eligibility ─────────────────

def test_topic_purity_high_when_all_sources_relevant_and_strong():
    gate = gate_sources("IFA 2026 클로징", [
        _item("IFA 2026 클로징 현장 스케치", published_at=_iso(NOW)),
        _item("IFA 2026 클로징 참가 기업 정리", published_at=_iso(NOW)),
    ])
    purity, _ = scoring.compute_topic_purity_score(gate)
    assert purity >= 90
    eligible, reason = scoring.compute_article_eligibility(gate, purity)
    assert eligible is True
    assert reason == ""


def test_topic_purity_zero_when_no_sources():
    gate = gate_sources("아무 토픽", [])
    purity, _ = scoring.compute_topic_purity_score(gate)
    assert purity == 0
    eligible, reason = scoring.compute_article_eligibility(gate, purity)
    assert eligible is False
    assert reason == "NO_RELEVANT_SOURCES"


def test_article_eligibility_blocked_by_gate_before_purity_checked():
    gate = gate_sources("IFA 2026 클로징", [_item("고윤정 이선빈 KGMA 참석")])
    eligible, reason = scoring.compute_article_eligibility(gate, topic_purity=100)
    assert eligible is False
    assert reason in ("NO_RELEVANT_SOURCES", "INSUFFICIENT_RELEVANT_SOURCES")


# ── compute_final_score ───────────────────────────────────────────────────────

def test_final_score_weights_blog_more_than_trend():
    # TrendScore 99, Relevance 0(=BlogScore 낮음)인 "IFA 2026 클로징" 시나리오
    high_trend_low_blog = scoring.compute_final_score(trend_score=99, blog_score=10)
    # TrendScore 70, BlogScore 85인 "쓸 만한" 시나리오
    balanced = scoring.compute_final_score(trend_score=70, blog_score=85)
    assert balanced > high_trend_low_blog


def test_final_score_formula():
    assert scoring.compute_final_score(trend_score=70, blog_score=85) == round(70 * 0.4 + 85 * 0.6)


def test_editorial_bonus_prioritizes_entertainment_and_click_signals():
    assert _editorial_bonus("연예", 30) > _editorial_bonus("정치", 0)
    assert _editorial_bonus("연예", 30) > _editorial_bonus("연예", 0)
    assert scoring.compute_final_score(70, 70, editorial_bonus=15) > scoring.compute_final_score(70, 70)


# ── Topic Deduplication ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cluster_topics_merges_via_llm_mapping():
    """LLM 클러스터링 결과를 그대로 신뢰해 '취업자 수 증가'/'청년 고용'을 한 cluster로 묶는다."""
    results = [
        {"topic": "취업자 수 증가", "category": "경제", "final_score": 68},
        {"topic": "청년 고용", "category": "경제", "final_score": 62},
        {"topic": "IFA 2026 클로징", "category": "IT", "final_score": 40},
    ]
    mapping = {"취업자 수 증가": "고용/청년", "청년 고용": "고용/청년", "IFA 2026 클로징": "IFA 2026 클로징"}
    with patch("app.trendlab.scoring._llm_cluster_map", new=AsyncMock(return_value=mapping)):
        clustered = await scoring.cluster_topics(results)

    by_topic = {r["topic"]: r for r in clustered}
    assert by_topic["취업자 수 증가"]["cluster"] == "고용/청년"
    assert by_topic["청년 고용"]["cluster"] == "고용/청년"
    # final_score가 더 높은 "취업자 수 증가"가 대표로 남고 "청년 고용"은 중복 처리된다
    assert by_topic["취업자 수 증가"]["is_duplicate"] is False
    assert by_topic["청년 고용"]["is_duplicate"] is True
    assert by_topic["IFA 2026 클로징"]["is_duplicate"] is False


@pytest.mark.asyncio
async def test_cluster_topics_falls_back_to_keyword_when_llm_fails():
    """LLM 클러스터링이 실패하면 키워드 Jaccard(같은 category + 단어 절반 이상 겹침)로 대체한다.
    (동의어를 이해하지 못하는 게 이 fallback의 알려진 한계 — 그래서 LLM 경로가 기본이다.)"""
    results = [
        {"topic": "손흥민 이적설 논란", "category": "스포츠", "final_score": 80},
        {"topic": "손흥민 이적설 정리", "category": "스포츠", "final_score": 50},
    ]
    with patch("app.trendlab.scoring._llm_cluster_map", new=AsyncMock(return_value=None)):
        clustered = await scoring.cluster_topics(results)
    by_topic = {r["topic"]: r for r in clustered}
    assert by_topic["손흥민 이적설 논란"]["cluster"] == by_topic["손흥민 이적설 정리"]["cluster"]
    assert by_topic["손흥민 이적설 정리"]["is_duplicate"] is True


# ── 통합: extract_topics()에서 Blog TOP10이 실제로 걸러지는지 ────────────────

def _mock_ner(event: str, category: str, example_title: str, count: int = 3):
    return {event: {"count": count, "category": category, "example_title": example_title}}


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_TEST4_high_trend_insufficient_relevance_excluded_from_blog_top10():
    """NER이 count=10(높은 Trend)으로 잡아도, 실제로 topic 문구와 매칭되는 기사가 1건뿐이면
    (나머지는 topic 단어와 아예 안 겹쳐 애초에 매칭조차 안 됨) relevant_source_count<2로
    Blog TOP10에서 제외된다 — Trend 점수와 무관하게."""
    fresh = NOW - timedelta(hours=2)
    items = [
        {"source": "naver", "title": "IFA 2026 클로징 현장 스케치", "published_at": _iso(fresh), "publisher": "a.com"},
        {"source": "naver", "title": "고윤정 이선빈 KGMA 참석", "published_at": _iso(fresh), "publisher": "b.com"},
        {"source": "naver", "title": "하나은행 브랜드평판 1위", "published_at": _iso(fresh), "publisher": "c.com"},
    ]
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(
        return_value=_mock_ner("IFA 2026 클로징", "IT", "IFA 2026 클로징 현장 스케치", count=10),
    )), patch("app.trendlab.analyzer._validate_topic", new=AsyncMock(return_value=True)), \
         patch("app.trendlab.scoring._llm_cluster_map", new=AsyncMock(return_value=None)):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert result == []  # 유일한 후보가 eligible=False(관련 source 1건뿐)라 blog_top10이 비어야 한다


@pytest.mark.asyncio
async def test_TEST5_relevant_topic_becomes_blog_candidate():
    fresh = NOW - timedelta(hours=2)
    items = [
        {"source": "naver", "title": "IFA 2026 클로징 현장 스케치", "published_at": _iso(fresh), "publisher": "a.com"},
        {"source": "naver", "title": "IFA 2026 클로징 참가 기업 정리", "published_at": _iso(fresh), "publisher": "b.com"},
        {"source": "naver", "title": "IFA 2026 클로징 신제품 총정리", "published_at": _iso(fresh), "publisher": "c.com"},
    ]
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(
        return_value=_mock_ner("IFA 2026 클로징", "IT", "IFA 2026 클로징 현장 스케치"),
    )), patch("app.trendlab.analyzer._validate_topic", new=AsyncMock(return_value=True)), \
         patch("app.trendlab.scoring._llm_cluster_map", new=AsyncMock(return_value=None)):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert len(result) == 1
    assert result[0]["article_eligible"] is True
    assert result[0]["relevant_source_count"] >= 2
    assert result[0]["final_score"] is not None


@pytest.mark.asyncio
async def test_TEST6_semantic_duplicate_topics_merge_in_full_pipeline():
    fresh = NOW - timedelta(hours=2)
    items = [
        {"source": "naver", "title": "청년 취업자 수 3개월 연속 증가", "published_at": _iso(fresh), "publisher": "a.com"},
        {"source": "naver", "title": "청년 취업자 수 통계청 발표", "published_at": _iso(fresh), "publisher": "b.com"},
        {"source": "naver", "title": "청년 고용률 역대 최고", "published_at": _iso(fresh), "publisher": "c.com"},
        {"source": "naver", "title": "청년 고용 개선 정책 효과", "published_at": _iso(fresh), "publisher": "d.com"},
    ]
    ner_result = {
        "취업자 수 증가": {"count": 3, "category": "경제", "example_title": "청년 취업자 수 3개월 연속 증가"},
        "청년 고용": {"count": 3, "category": "경제", "example_title": "청년 고용률 역대 최고"},
    }
    mapping = {"취업자 수 증가": "고용/청년", "청년 고용": "고용/청년"}
    with patch("app.trendlab.analyzer._ner_event_call", new=AsyncMock(return_value=ner_result)), \
         patch("app.trendlab.analyzer._validate_topic", new=AsyncMock(return_value=True)), \
         patch("app.trendlab.scoring._llm_cluster_map", new=AsyncMock(return_value=mapping)):
        with patch("app.trendlab.analyzer.datetime", _FixedDatetime):
            result = await extract_topics(items)

    assert len(result) == 1  # 두 topic 중 하나만 살아남는다
