import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.speech_service import (
    _CATEGORY_SCORE_FIELDS,
    _DEFAULT_SCORE_FIELDS,
    _score_fields_for,
    generate_speech_feedback,
)

ALL_CATEGORIES = [
    "extempore",
    "randomQuestion",
    "entryInterview",
    "experiencedInterview",
    "presentation",
    "debate",
]


def _mock_response(payload: dict):
    return SimpleNamespace(text=json.dumps(payload))


def _fake_client(payload: dict):
    client = SimpleNamespace()
    client.aio = SimpleNamespace()
    client.aio.models = SimpleNamespace()
    client.aio.models.generate_content = AsyncMock(return_value=_mock_response(payload))
    return client


def _gemini_payload_for(category: str, score_value: int = 80) -> dict:
    fields = _score_fields_for(category)
    return {
        "overallScore": 82,
        "categoryScores": {key: score_value for key, _ in fields},
        "summary": "핵심 의견은 명확하지만 구체적인 사례가 부족합니다.",
        "strengths": [
            {"title": "주장이 명확합니다", "description": "입장을 처음에 명확히 제시했습니다."}
        ],
        "improvements": [
            {
                "title": "구체적인 사례를 추가하세요",
                "description": "실제 경험이 없어 설득력이 약합니다.",
                "priority": "high",
            }
        ],
        "betterAnswer": "개선된 답변 전체 텍스트",
        "fillerWords": [{"word": "음", "count": 4}],
        "nextPractice": "같은 질문에 실제 경험을 하나 포함해서 다시 답해보세요.",
    }


# --- score field mapping (no network) ---


def test_default_score_fields_used_for_extempore_and_random():
    assert _score_fields_for("extempore") == _DEFAULT_SCORE_FIELDS
    assert _score_fields_for("randomQuestion") == _DEFAULT_SCORE_FIELDS


def test_unknown_category_falls_back_to_default_fields():
    assert _score_fields_for("not_a_real_category") == _DEFAULT_SCORE_FIELDS


def test_each_declared_category_has_distinct_field_set():
    for category, fields in _CATEGORY_SCORE_FIELDS.items():
        keys = [key for key, _ in fields]
        assert len(keys) == len(set(keys)), f"duplicate keys in {category}"
        assert all(0 < len(desc) for _, desc in fields)


# --- generate_speech_feedback per category (Gemini call mocked) ---


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ALL_CATEGORIES)
async def test_generate_speech_feedback_returns_category_scores(category):
    payload = _gemini_payload_for(category)
    with patch(
        "services.speech_service._make_client",
        return_value=_fake_client(payload),
    ):
        result = await generate_speech_feedback(
            question="자유학기제는 효과적이라고 생각하나요?",
            category=category,
            transcript="저는 자유학기제가 학생들에게 도움이 된다고 생각합니다.",
            duration_seconds=40,
        )

    expected_keys = {key for key, _ in _score_fields_for(category)}

    # 기존 필드는 그대로 존재해야 한다 (additive change).
    assert result["overallScore"] == 82
    assert result["summary"]
    assert result["betterAnswer"]
    assert result["nextPractice"]
    assert result["strengths"]
    assert result["improvements"]
    assert result["fillerWords"] == [{"word": "음", "count": 4}]
    assert "speechAnalysis" in result

    # 새 필드: 해당 category에 맞는 항목만, 0~100 정수.
    assert set(result["categoryScores"].keys()) == expected_keys
    for value in result["categoryScores"].values():
        assert isinstance(value, int)
        assert 0 <= value <= 100


@pytest.mark.asyncio
async def test_out_of_range_scores_are_clamped():
    fields = _score_fields_for("presentation")
    payload = _gemini_payload_for("presentation")
    payload["overallScore"] = 150  # 스키마를 어긴 비정상 값
    payload["categoryScores"] = {key: -20 for key, _ in fields}

    with patch(
        "services.speech_service._make_client",
        return_value=_fake_client(payload),
    ):
        result = await generate_speech_feedback(
            question="신제품 출시 계획을 발표해보세요.",
            category="presentation",
            transcript="오늘 발표할 내용은...",
            duration_seconds=60,
        )

    assert result["overallScore"] == 100
    assert all(v == 0 for v in result["categoryScores"].values())


@pytest.mark.asyncio
async def test_missing_category_scores_falls_back_to_overall_score():
    payload = _gemini_payload_for("debate")
    del payload["categoryScores"]  # Gemini가 스키마를 어기고 필드를 누락한 경우

    with patch(
        "services.speech_service._make_client",
        return_value=_fake_client(payload),
    ):
        result = await generate_speech_feedback(
            question="원격 근무는 확대되어야 하는가?",
            category="debate",
            transcript="저는 원격 근무 확대에 찬성합니다.",
            duration_seconds=50,
        )

    expected_keys = {key for key, _ in _score_fields_for("debate")}
    assert set(result["categoryScores"].keys()) == expected_keys
    assert all(v == result["overallScore"] for v in result["categoryScores"].values())
