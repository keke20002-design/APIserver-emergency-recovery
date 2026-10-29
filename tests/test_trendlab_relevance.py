"""TrendLab Source Relevance Gate 테스트.

실제로 관찰된 사고 사례: topic "IFA 2026 클로징"에 고윤정/이선빈/KGMA/아시안게임/
광명시 책축제/하나은행/트럼프/이란처럼 전혀 무관한 뉴스가 source_items로 섞여 들어가고,
그걸 근거로 "오늘의 다른 뉴스"를 나열하는 글이 생성됐다. 이 테스트는:
  1) 근본 원인(analyzer._topic_matches의 느슨한 any() 매칭)이 고쳐졌는지
  2) 생성 직전 Source Relevance Gate가 무관한 source를 실제로 걸러내는지
를 검증한다.
"""
from app.trendlab.analyzer import _topic_matches
from app.trendlab.relevance import (
    MIN_AVERAGE_RELEVANCE,
    MIN_RELEVANT_SOURCE_COUNT,
    STRONG_RELEVANT_THRESHOLD,
    gate_sources,
    score_source,
)

TOPIC = "IFA 2026 클로징"


def _item(title: str, desc: str = "", published_at: str | None = None) -> dict:
    return {
        "source": "naver", "title": title, "description": desc,
        "published_at": published_at, "publisher": "a.com", "url": "",
    }


UNRELATED_TITLES = [
    "고윤정 이선빈 KGMA 참석",
    "김환 아시안게임 중계",
    "광명시 책축제 개막",
    "가업승계 세미나 개최",
    "전남광주통합특별시 추진",
    "원주 삼토페스티벌 성료",
    "하나은행 브랜드평판 1위",
    "트럼프 이란 관련 발언",
]


# ── analyzer._topic_matches (근본 원인 수정) ─────────────────────────────────

def test_topic_matches_rejects_bare_year_overlap():
    """topic 단어 중 숫자(연도)만 겹치는 무관한 제목은 매칭되면 안 된다."""
    assert _topic_matches(TOPIC, "OO 아시안게임 2026 개막식") is False


def test_topic_matches_accepts_real_match():
    assert _topic_matches(TOPIC, "IFA 2026 베를린서 클로징 행사 개최") is True


def test_topic_matches_accepts_partial_core_word_match():
    # "IFA 2026 클로징" 중 "IFA"·"2026"만 있어도(3단어 중 2개=len-1) 통과
    assert _topic_matches(TOPIC, "IFA 2026 참가 기업 명단 공개") is True


def test_topic_matches_two_word_topic_requires_core_word():
    assert _topic_matches("손흥민 이적설", "2026년 예산안 발표") is False


def test_topic_matches_two_word_topic_numeric_word_alone_insufficient():
    """2단어 topic이라도 숫자 단어 하나만 겹치는 건 매칭으로 인정하지 않는다."""
    assert _topic_matches("아시안게임 2026", "2026 예산안 편성 발표") is False
    assert _topic_matches("아시안게임 2026", "2026 아시안게임 참가국 발표") is True


# ── score_source ─────────────────────────────────────────────────────────────

def test_score_source_exact_phrase_scores_highest():
    result = score_source(TOPIC, _item("IFA 2026 클로징 현장 스케치"))
    assert result["relevance_score"] >= 90
    assert result["directly_supports_topic"] is True


def test_score_source_unrelated_title_scores_below_threshold():
    result = score_source(TOPIC, _item("고윤정 이선빈 KGMA 참석"))
    assert result["relevance_score"] < 50
    assert result["directly_supports_topic"] is False


def test_score_source_year_only_overlap_scores_zero():
    result = score_source(TOPIC, _item("OO 아시안게임 2026 개막식"))
    assert result["relevance_score"] == 0


def test_score_source_reason_is_grounded_not_abstract():
    """relevance_reason은 실제 겹친 단어를 근거로 들어야 한다 — 추상적 설명 금지."""
    result = score_source(TOPIC, _item("고윤정 이선빈 KGMA 참석"))
    for banned in ("연관되어 있을 수 있습니다", "산업 전반의 흐름", "도움이 될 수 있습니다"):
        assert banned not in result["relevance_reason"]


# ── gate_sources — 사용자 스펙 TEST1~4 ───────────────────────────────────────

def test_TEST1_no_relevant_sources_blocks():
    sources = [_item(t) for t in UNRELATED_TITLES]
    gate = gate_sources(TOPIC, sources)
    assert gate.status == "blocked"
    assert gate.block_reason == "NO_RELEVANT_SOURCES"
    assert gate.relevant_source_count == 0
    assert gate.average_relevance == 0


def test_TEST2_relevant_sources_used_unrelated_excluded():
    relevant = [
        _item("IFA 2026 공식 개막", published_at="2026-09-06T00:00:00Z"),
        _item("IFA 2026 클로징 현장 스케치", published_at="2026-09-08T00:00:00Z"),
        _item("IFA 2026 참가 기업 신제품 정리", published_at="2026-09-07T00:00:00Z"),
    ]
    sources = relevant + [_item(t) for t in UNRELATED_TITLES]
    gate = gate_sources(TOPIC, sources)

    assert gate.status == "ok"
    assert gate.relevant_source_count == 3
    kept_titles = {s["title"] for s in gate.relevant_sources}
    assert kept_titles == {s["title"] for s in relevant}
    assert not (kept_titles & set(UNRELATED_TITLES))


def test_TEST3_single_relevant_source_blocks():
    sources = [_item("IFA 2026 클로징 현장 스케치")] + [_item(t) for t in UNRELATED_TITLES]
    gate = gate_sources(TOPIC, sources)
    assert gate.status == "blocked"
    assert gate.relevant_source_count == 1
    assert gate.block_reason == "INSUFFICIENT_RELEVANT_SOURCES"


def test_TEST4_five_relevant_sources_passes():
    relevant = [_item(f"IFA 2026 클로징 소식 {i}") for i in range(5)]
    gate = gate_sources(TOPIC, relevant)
    assert gate.status == "ok"
    assert gate.relevant_source_count == 5
    assert gate.average_relevance >= MIN_AVERAGE_RELEVANCE


def test_gate_log_dict_has_required_fields():
    sources = [_item(t) for t in UNRELATED_TITLES]
    gate = gate_sources(TOPIC, sources)
    log = gate.to_log_dict(TOPIC)
    for key in ("topic", "source_count", "relevant_source_count", "average_relevance",
                "status", "block_reason", "latest_relevant_source"):
        assert key in log
    assert log["status"] == "blocked"
    assert log["source_count"] == len(UNRELATED_TITLES)


def test_gate_blocks_when_no_strong_relevant_source():
    """관련 source가 min_relevant_count/avg는 넘겨도 '핵심 source'(>=90점)가 하나도 없으면 차단."""
    sources = [
        _item("IFA 2026 참가 기업 명단 공개"),   # 78점 (핵심 단어 대부분, 문구 완전일치 아님)
        _item("IFA 2026 부대행사 일정 공개"),     # 78점
    ]
    gate = gate_sources(TOPIC, sources)
    assert all(50 <= s["relevance_score"] < STRONG_RELEVANT_THRESHOLD for s in gate.relevant_sources)
    assert gate.status == "blocked"
    assert gate.block_reason == "LOW_SOURCE_RELEVANCE"
    assert gate.strong_relevant_source_count == 0


def test_gate_thresholds_match_spec():
    assert MIN_RELEVANT_SOURCE_COUNT == 2
    assert MIN_AVERAGE_RELEVANCE == 65.0
