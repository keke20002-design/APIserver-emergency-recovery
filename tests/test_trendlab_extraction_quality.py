"""TrendLab Extraction Quality — timeline/importance/source_id 확장, richness 보정 4단계,
extractor → generator 데이터 흐름 검증.

실제 Gemini API는 비용 때문에 호출하지 않는다. 대신 실제 응답과 동일한 schema를 가진 현실적인
fixture(raw JSON)로 google.genai.Client를 모킹해서, 원문 → fact extraction → richness →
article generation input 순서의 전체 흐름이 코드 상으로 끊기지 않는지 검증한다.

CASE A: 논란/공방 기사 (사회) — sides/unconfirmed가 채워짐, entities는 비어있는 게 정상.
CASE B: 단순 연예 뉴스 (아이유 신곡 발표) — sides/unconfirmed는 비어있지만 entities/background/
        timeline이 채워져야 한다. 이게 비어서 "facts 몇 개만 사용 → 짧은 article"로 돌아가는 게
        이번 수정 전 실제로 있었던 버그였다.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.trendlab import fact_extractor as fe
from app.trendlab import issue_generator as ig
from app.trendlab.relevance import RelevanceGateResult


def _source_items() -> list[dict]:
    return [
        {"source": "naver", "title": "아이유, 1년 만에 신곡 발표…경찰 변신·김영옥 출연 화제",
         "description": "", "published_at": "2026-09-08T09:00:00+00:00", "publisher": "MBC뉴스", "url": ""},
        {"source": "naver", "title": "아이유 신곡 뮤직비디오 공개, 경찰役 눈길",
         "description": "", "published_at": "2026-09-08T10:00:00+00:00", "publisher": "이투데이", "url": ""},
        {"source": "naver", "title": "아이유 신곡에 김영옥 카메오 출연 화제",
         "description": "", "published_at": "2026-09-08T11:00:00+00:00", "publisher": "스포츠서울", "url": ""},
        {"source": "youtube", "title": "아이유 신곡 뮤직비디오 풀버전",
         "description": "", "published_at": "2026-09-08T12:00:00+00:00", "publisher": "1theK", "url": ""},
    ]


# CASE B — 실제 Gemini 2.5 flash 응답과 동일한 schema를 가진 현실적 fixture(단순 연예 뉴스).
# facts[5]는 facts[0]과 사실상 같은 내용을 표현만 바꿔 쓴 근접 중복 — dedupe 검증용으로 일부러 넣음.
IU_RAW_RESPONSE = {
    "facts": [
        {"fact": "아이유가 9월 8일 신곡을 발표했다", "importance": 95, "source_id": "1"},
        {"fact": "신곡 발표는 이전 정규 앨범 이후 1년 만이다", "importance": 90, "source_id": "1"},
        {"fact": "신곡 발표와 동시에 뮤직비디오가 공개됐다", "importance": 85, "source_id": "2"},
        {"fact": "아이유는 뮤직비디오에서 경찰 역할을 연기했다", "importance": 80, "source_id": "2"},
        {"fact": "뮤직비디오에 배우 김영옥이 카메오로 출연했다", "importance": 78, "source_id": "3"},
        {"fact": "9월 8일에 신곡을 발표했다", "importance": 60, "source_id": "1"},
    ],
    "statements": [],
    "unconfirmed": [],
    "sides": [],
    "controversy_core": "",
    "why_trending": "1년 만의 컴백인 데다 경찰 컨셉의 파격적인 뮤직비디오와 김영옥의 카메오 출연이 "
                     "겹치며 보도가 확산됐다",
    "current_reaction": "",
    "qa": [
        {"question": "신곡은 언제 공개됐나?", "answer": "9월 8일 공개됐다"},
        {"question": "뮤직비디오에는 누가 출연했나?", "answer": "김영옥이 카메오로 출연했다"},
    ],
    "entities": [
        {"name": "김영옥", "detail": "뮤직비디오에 카메오로 출연했다", "source_id": "3"},
        {"name": "유인나", "detail": "신곡 관련 보도에서 함께 언급된 인물이다", "source_id": "1"},
    ],
    "background": [
        {"text": "아이유의 이전 정규 앨범은 약 1년 전 발매됐다", "source_id": "1"},
    ],
    "timeline": [
        {"date": "1년 전", "event": "이전 정규 앨범 발매", "source_id": "1"},
        {"date": "9월 8일", "event": "신곡 및 뮤직비디오 공개", "source_id": "2"},
    ],
}

# CASE A — 논란/공방 기사 fixture. entities는 실제로 구체적 역할이 확인된 인물이 없어 빈 배열이
# 정상이다(억지로 채우면 안 된다는 규칙의 반대쪽 검증).
CONTROVERSY_RAW_RESPONSE = {
    "facts": [
        {"fact": "8월 26일 경찰이 A씨 체포 사실을 공개했다", "importance": 95, "source_id": "1"},
        {"fact": "체포는 4월 발생한 사건에 대한 것이다", "importance": 85, "source_id": "1"},
    ],
    "statements": [
        {"speaker": "A씨 측 변호인", "type": "반박", "content": "혐의를 인정할 수 없다고 밝혔다",
         "date": "8월 26일"},
        {"speaker": "경찰 관계자", "type": "보도", "content": "추가 조사가 진행 중이라고 밝혔다",
         "date": "8월 26일"},
    ],
    "unconfirmed": ["정확한 범행 동기는 아직 확인되지 않았다"],
    "sides": [
        {"name": "경찰", "stance": "주장", "points": ["체포 당시 혐의를 뒷받침하는 정황이 있었다"]},
        {"name": "A씨 측", "stance": "반박", "points": ["혐의를 인정할 수 없다"]},
    ],
    "controversy_core": "체포 근거의 적절성을 두고 이견이 있다",
    "why_trending": "체포 사실이 사건 발생 4개월 만에 뒤늦게 공개되면서 논란이 됐다",
    "current_reaction": "",
    "qa": [],
    "entities": [],
    "background": [],
    "timeline": [
        {"date": "4월", "event": "사건 발생", "source_id": "1"},
        {"date": "8월 26일", "event": "경찰이 체포 사실 공개", "source_id": "1"},
    ],
}


def _mock_genai_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps(payload, ensure_ascii=False)
    return resp


# ── analyze_event: 새 필드 파싱 + importance/source_id + dedupe ─────────────

@pytest.mark.asyncio
async def test_analyze_event_parses_new_fields_for_entertainment_case():
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=_mock_genai_response(IU_RAW_RESPONSE))
    with patch("google.genai.Client", return_value=fake_client):
        analysis = await fe.analyze_event("아이유 신곡 발표", "연예", _source_items())

    assert analysis["entities"], "entities가 비어있으면 안 됨 — 김영옥/유인나가 추출돼야 한다"
    names = {e["name"] for e in analysis["entities"]}
    assert names == {"김영옥", "유인나"}

    assert analysis["background"], "background가 비어있으면 안 됨"
    assert "1년" in analysis["background"][0]["text"]

    assert len(analysis["timeline"]) == 2, "timeline 2건이 모두 살아있어야 함"

    importances = [f["importance"] for f in analysis["facts"]]
    assert importances == sorted(importances, reverse=True), "facts는 importance 내림차순 정렬"
    assert all("source_id" in f for f in analysis["facts"])

    fact_texts = [f["fact"] for f in analysis["facts"]]
    assert len(fact_texts) < 6, (
        "근접 중복 fact('9월 8일 신곡 발표' 두 번)가 제거되지 않음 — 정보량 중복 계산 버그"
    )


@pytest.mark.asyncio
async def test_analyze_event_political_case_has_sides_and_timeline_but_no_forced_entities():
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(
        return_value=_mock_genai_response(CONTROVERSY_RAW_RESPONSE)
    )
    with patch("google.genai.Client", return_value=fake_client):
        analysis = await fe.analyze_event("A씨 체포 논란", "사회", _source_items())

    assert analysis["sides"]
    assert analysis["unconfirmed"]
    assert analysis["timeline"]
    assert analysis["entities"] == [], "구체적 역할이 없는 사건에 entities를 억지로 채우면 안 됨"


@pytest.mark.asyncio
async def test_entertainment_case_richness_is_not_low_thanks_to_entities_and_background():
    """회귀 테스트: entities/background/timeline이 실제로 있는데도 richness가 LOW로 나오면,
    facts 몇 개만으로 판단하던 기존 버그가 재발한 것이다."""
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=_mock_genai_response(IU_RAW_RESPONSE))
    with patch("google.genai.Client", return_value=fake_client):
        analysis = await fe.analyze_event("아이유 신곡 발표", "연예", _source_items())

    richness = fe.compute_richness(_source_items(), analysis)
    assert richness["level"] != "LOW", f"richness={richness}"
    assert richness["min_chars"] >= 1000


# ── compute_richness: 사용자가 제시한 4단계 보정 예시 ────────────────────────

def _analysis(facts=0, entities=0, background=0, timeline=0, qa=0, statements=0, importance=70) -> dict:
    return {
        "facts": [{"fact": f"f{i}", "importance": importance, "source_id": "1"} for i in range(facts)],
        "statements": [{"speaker": "a", "type": "주장", "content": "x", "date": ""} for _ in range(statements)],
        "entities": [{"name": f"e{i}", "detail": "d", "source_id": ""} for i in range(entities)],
        "background": [{"text": f"b{i}", "source_id": ""} for i in range(background)],
        "timeline": [{"date": "1", "event": f"t{i}", "source_id": ""} for i in range(timeline)],
        "qa": [{"question": "q", "answer": "a"} for _ in range(qa)],
    }


def _sources(n: int, independent: int | None = None) -> list[dict]:
    independent = independent or n
    return [{"source": "naver", "publisher": f"pub{i % independent}"} for i in range(n)]


def test_richness_low():
    r = fe.compute_richness(_sources(1), _analysis(facts=3, entities=1))
    assert r["level"] == "LOW"
    assert (r["min_chars"], r["max_chars"]) == (600, 800)


def test_richness_medium():
    r = fe.compute_richness(_sources(4), _analysis(facts=7, entities=4, background=1, timeline=1))
    assert r["level"] == "MEDIUM"
    assert (r["min_chars"], r["max_chars"]) == (1000, 1400)


def test_richness_high():
    r = fe.compute_richness(
        _sources(8, independent=5), _analysis(facts=12, entities=6, background=3, timeline=4, qa=5),
    )
    assert r["level"] == "HIGH"
    assert (r["min_chars"], r["max_chars"]) == (1400, 1900)


def test_richness_very_high():
    r = fe.compute_richness(
        _sources(15, independent=8),
        _analysis(facts=20, entities=10, background=5, timeline=8, qa=3, statements=3),
    )
    assert r["level"] == "VERY_HIGH"
    assert (r["min_chars"], r["max_chars"]) == (1700, 2300)


# ── extraction quality: 양이 아니라 질을 반영하는지 ──────────────────────────

def test_extraction_quality_rewards_diversity_over_raw_fact_count():
    thin_but_many_facts = fe.compute_extraction_quality(
        _sources(3), _analysis(facts=10, entities=0, background=0, timeline=0, importance=40),
    )
    rich_and_diverse = fe.compute_extraction_quality(
        _sources(3), _analysis(facts=5, entities=3, background=2, timeline=2, importance=90),
    )
    assert rich_and_diverse["extraction_quality_score"] > thin_but_many_facts["extraction_quality_score"]


# ── dedupe helpers ───────────────────────────────────────────────────────────

def test_dedupe_by_similarity_merges_near_duplicate_facts():
    facts = [
        {"fact": "아이유가 9월 8일 신곡을 발표했다", "importance": 95, "source_id": "1"},
        {"fact": "9월 8일에 신곡을 발표했다", "importance": 60, "source_id": "1"},
    ]
    deduped = fe._dedupe_by_similarity(facts, "fact")
    assert len(deduped) == 1
    assert deduped[0]["importance"] == 95, "클러스터 안에서는 importance가 더 높은 쪽을 대표로 남김"


def test_dedupe_entities_keeps_longer_detail():
    entities = [
        {"name": "김영옥", "detail": "출연", "source_id": ""},
        {"name": "김영옥", "detail": "뮤직비디오에 카메오로 출연했다", "source_id": "3"},
    ]
    deduped = fe._dedupe_entities(entities)
    assert len(deduped) == 1
    assert deduped[0]["detail"] == "뮤직비디오에 카메오로 출연했다"


# ── issue_generator._facts_block: importance 필터링 ─────────────────────────

def test_facts_block_drops_low_importance_when_enough_significant_facts_exist():
    facts = [
        {"fact": "핵심 사실", "importance": 95, "source_id": "1"},
        {"fact": "중요 사실1", "importance": 75, "source_id": "1"},
        {"fact": "중요 사실2", "importance": 70, "source_id": "1"},
        {"fact": "생략 가능한 사실", "importance": 20, "source_id": "1"},
    ]
    block = ig._facts_block(facts)
    assert "생략 가능한 사실" not in block
    assert "핵심 사실" in block


def test_facts_block_keeps_all_when_all_importance_is_low():
    """정보 자체가 적은(LOW richness) 사건이면 importance가 낮아도 정보 손실 없이 전부 살린다."""
    facts = [
        {"fact": "유일한 사실1", "importance": 30, "source_id": "1"},
        {"fact": "유일한 사실2", "importance": 20, "source_id": "1"},
    ]
    block = ig._facts_block(facts)
    assert "유일한 사실1" in block and "유일한 사실2" in block


# ── generate_issue_post: extractor → generator 데이터 흐름 (가장 중요) ──────

@pytest.mark.asyncio
async def test_generate_issue_post_forwards_entities_background_timeline_to_prompt():
    """fact_extractor가 뽑은 entities/background/timeline이 실제로 Gemini에 보내는 프롬프트
    (contents)까지 살아서 전달되는지 확인한다 — 이게 끊기면 이번 수정 전체가 무의미하다."""
    generated_content = (
        '<h2>3줄 요약</h2><p>아이유가 신곡을 발표했다.</p>'
        '<h2>세부 내용</h2><p>9월 8일 발표된 신곡은 1년 만의 컴백이다. '
        '뮤직비디오에서 아이유는 경찰 역할을 맡았다.</p>'
        '<h2>관련 인물</h2><p>김영옥은 뮤직비디오에 카메오로 출연해 화제를 모았다. '
        '유인나도 관련 보도에서 언급됐다.</p>'
        '<h2>배경</h2><p>아이유의 이전 정규 앨범은 약 1년 전 발매됐다.</p>'
    )
    gemini_payload = {
        "title": "아이유, 1년 만에 신곡 발표…경찰 변신·김영옥 출연 화제",
        "meta_description": "아이유의 신곡 발표 소식",
        "tags": ["아이유"],
        "summary": ["아이유 신곡 발표", "경찰 컨셉", "김영옥 출연"],
        "content": generated_content,
    }

    async def fake_analyze_event(topic, category, source_items):
        return {
            "facts": [
                {"fact": "아이유가 9월 8일 신곡을 발표했다", "importance": 95, "source_id": "1"},
                {"fact": "1년 만의 컴백이다", "importance": 90, "source_id": "1"},
            ],
            "statements": [], "unconfirmed": [], "sides": [],
            "controversy_core": "",
            "why_trending": "1년 만의 컴백과 파격적인 뮤직비디오로 화제가 됐다",
            "current_reaction": "",
            "qa": [{"question": "신곡은 언제 공개됐나?", "answer": "9월 8일 공개됐다"}],
            "entities": [
                {"name": "김영옥", "detail": "뮤직비디오에 카메오로 출연했다", "source_id": "3"},
                {"name": "유인나", "detail": "신곡 관련 보도에서 함께 언급된 인물이다", "source_id": "1"},
            ],
            "background": [{"text": "아이유의 이전 정규 앨범은 약 1년 전 발매됐다", "source_id": "1"}],
            "timeline": [{"date": "9월 8일", "event": "신곡 및 뮤직비디오 공개", "source_id": "2"}],
        }

    captured_prompts: list[str] = []

    async def fake_generate_content(model, contents, config):
        captured_prompts.append(contents)
        return _mock_genai_response(gemini_payload)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    fake_gate = RelevanceGateResult(
        status="ok", relevant_sources=_source_items(), source_count=4, relevant_source_count=4,
        strong_relevant_source_count=2, average_relevance=90.0,
    )

    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
         patch("app.trendlab.fact_extractor.analyze_event", side_effect=fake_analyze_event), \
         patch.object(ig, "gate_sources", return_value=fake_gate), \
         patch("google.genai.Client", return_value=fake_client):
        result = await ig.generate_issue_post("아이유 신곡 발표", "연예", _source_items())

    assert captured_prompts, "Gemini에 프롬프트가 전달되지 않음"
    prompt = captured_prompts[0]
    assert "김영옥" in prompt and "뮤직비디오에 카메오로 출연했다" in prompt, "entities가 프롬프트에 없음"
    assert "유인나" in prompt
    assert "이전 정규 앨범은 약 1년 전" in prompt, "background가 프롬프트에 없음"
    assert "9월 8일: 신곡 및 뮤직비디오 공개" in prompt, "timeline이 프롬프트에 없음"
    assert result["title"]
    assert result["content"]
