import json
import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"

_CATEGORY_RUBRICS: dict[str, str] = {
    "entryInterview": """- 질문 적합성
- 답변 구조
- 구체성
- 지원자의 경험
- 직무 관련성""",
    "experiencedInterview": """- 문제 상황
- 행동
- 결과
- 성과
- 수치/근거
- 직무 전문성""",
    "presentation": """- 핵심 메시지
- 구조
- 설명력
- 청중 관점
- 결론""",
    "debate": """- 주장
- 근거
- 논리
- 반론 대응
- 설득력""",
}

_DEFAULT_RUBRIC = """- 질문 적합성
- 논리성
- 구체성
- 답변 구성
- 표현력"""

# 1차↔2차 재연습 비교(스피치플러스)에서 항목별 점수를 비교할 수 있도록 하는 세부
# 평가 항목. category마다 항목 구성이 다르므로 category별로 다른 key 집합을 쓴다.
# 같은 category는 항상 같은 key 집합/설명을 써야 재연습 비교가 의미를 가진다 —
# 이 리스트를 category별로 함부로 바꾸지 말 것.
_DEFAULT_SCORE_FIELDS: list[tuple[str, str]] = [
    ("questionFit", "질문 적합성 — 질문의 의도에 맞게 답했는가"),
    ("logic", "논리성 — 주장-이유-근거-결론의 흐름이 자연스러운가"),
    ("specificity", "구체성 — 실제 사례나 경험이 포함되어 있는가"),
    ("structure", "답변 구성 — 도입-전개-결론 구조가 잘 갖춰져 있는가"),
    ("expression", "표현력 — 문장이 명확하고 자연스러운가"),
]

_CATEGORY_SCORE_FIELDS: dict[str, list[tuple[str, str]]] = {
    "entryInterview": [
        ("questionFit", "질문 적합성 — 질문의 의도에 맞게 답했는가"),
        ("logic", "논리성 — 주장-이유-근거-결론의 흐름이 자연스러운가"),
        ("specificity", "구체성 — 실제 사례나 경험이 포함되어 있는가"),
        ("experience", "경험 활용 — 지원자 자신의 경험을 답변에 활용했는가"),
        ("structure", "답변 구성 — 도입-전개-결론 구조가 잘 갖춰져 있는가"),
    ],
    "experiencedInterview": [
        ("questionFit", "질문 적합성 — 질문의 의도에 맞게 답했는가"),
        ("jobRelevance", "경력/직무 연관성 — 답변이 지원 직무와 실제로 관련이 있는가"),
        ("specificity", "구체적인 성과 — 수치나 결과 등 구체적인 성과가 드러나는가"),
        ("achievement", "성과 — 본인이 이뤄낸 결과가 명확한가"),
        ("problemSolving", "문제 해결력 — 문제 상황을 어떻게 해결했는지 드러나는가"),
        ("structure", "답변 구성 — 상황-행동-결과 흐름이 잘 갖춰져 있는가"),
    ],
    "presentation": [
        ("coreMessage", "핵심 메시지 — 전달하려는 핵심이 명확한가"),
        ("structure", "발표 구성 — 서론-본론-결론 흐름이 잘 갖춰져 있는가"),
        ("logic", "논리성 — 주장과 근거의 연결이 자연스러운가"),
        ("explanation", "설명력 — 내용을 이해하기 쉽게 설명했는가"),
        ("specificity", "구체성 — 예시나 데이터로 뒷받침되는가"),
    ],
    "debate": [
        ("claimClarity", "주장 명확성 — 자신의 입장이 분명하게 드러나는가"),
        ("evidence", "근거 — 주장을 뒷받침하는 근거가 있는가"),
        ("logic", "논리성 — 근거에서 결론까지의 추론이 타당한가"),
        ("rebuttal", "반론 대응 — 예상 반론에 대한 고려가 있는가"),
        ("persuasiveness", "설득력 — 전체적으로 설득력이 있는가"),
    ],
}


def _score_fields_for(category: str) -> list[tuple[str, str]]:
    """extempore/randomQuestion(그리고 알 수 없는 category)은 기본 5항목을 쓴다 —
    _CATEGORY_RUBRICS의 기본 rubric(_DEFAULT_RUBRIC)과 항목이 1:1로 대응된다."""
    return _CATEGORY_SCORE_FIELDS.get(category, _DEFAULT_SCORE_FIELDS)


def _make_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured on server")
    return genai.Client(api_key=api_key)


def _compute_speech_analysis(transcript: str, duration_seconds: int) -> dict:
    """단순 산술 통계는 Gemini가 아니라 서버에서 직접 계산한다."""
    char_count = len(transcript)
    cpm = round(char_count / (duration_seconds / 60)) if duration_seconds > 0 else 0
    return {
        "durationSeconds": duration_seconds,
        "characterCount": char_count,
        "estimatedCharactersPerMinute": cpm,
    }


def _build_response_schema(score_fields: list[tuple[str, str]]) -> types.Schema:
    category_scores_properties = {
        key: types.Schema(type=types.Type.INTEGER) for key, _ in score_fields
    }
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "overallScore": types.Schema(type=types.Type.INTEGER),
            "categoryScores": types.Schema(
                type=types.Type.OBJECT,
                properties=category_scores_properties,
                required=[key for key, _ in score_fields],
            ),
            "summary": types.Schema(type=types.Type.STRING),
            "strengths": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "title": types.Schema(type=types.Type.STRING),
                        "description": types.Schema(type=types.Type.STRING),
                    },
                    required=["title", "description"],
                ),
            ),
            "improvements": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "title": types.Schema(type=types.Type.STRING),
                        "description": types.Schema(type=types.Type.STRING),
                        "priority": types.Schema(type=types.Type.STRING),
                    },
                    required=["title", "description", "priority"],
                ),
            ),
            "betterAnswer": types.Schema(type=types.Type.STRING),
            "fillerWords": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "word": types.Schema(type=types.Type.STRING),
                        "count": types.Schema(type=types.Type.INTEGER),
                    },
                    required=["word", "count"],
                ),
            ),
            "nextPractice": types.Schema(type=types.Type.STRING),
        },
        required=[
            "overallScore",
            "categoryScores",
            "summary",
            "strengths",
            "improvements",
            "betterAnswer",
            "fillerWords",
            "nextPractice",
        ],
    )


async def generate_speech_feedback(
    question: str,
    category: str,
    transcript: str,
    duration_seconds: int,
) -> dict:
    """스피치플러스 — 사용자의 답변(transcript)을 Gemini로 평가해 구조화된 피드백을 반환.

    실패 시 예외를 그대로 올려보낸다 — 호출부(route)에서 500으로 변환.
    """
    rubric = _CATEGORY_RUBRICS.get(category, _DEFAULT_RUBRIC)
    score_fields = _score_fields_for(category)
    score_fields_text = "\n".join(f"- {key}: {desc}" for key, desc in score_fields)
    score_example = ", ".join(f'"{key}": 80' for key, _ in score_fields)

    prompt = f"""너는 한국어 스피치 코치다.

사용자의 답변을 평가하고 구체적인 개선 방법을 제공한다.

[질문]
{question}

[연습 유형]
{category}

[사용자 답변]
{transcript}

[녹음 시간]
{duration_seconds}초

다음 기준으로 종합 평가(summary/strengths/improvements)를 작성한다.
{rubric}

세부 항목 점수(categoryScores)는 이 연습 유형({category})에서 정확히 아래 항목만 포함한다.
다른 항목을 추가하거나 이름을 바꾸지 않는다.
{score_fields_text}

각 세부 항목 점수는 0~100 사이 정수로 평가한다.

중요 규칙:
- 사용자의 실제 답변을 근거로 평가한다.
- 근거 없는 칭찬을 하지 않는다.
- 개선점은 최대 3개만 제시한다.
- 가장 중요한 개선점을 priority="high"로 지정한다. 나머지는 "medium" 또는 "low".
- betterAnswer는 사용자의 원래 의도와 내용을 유지하면서 더 나은 버전으로 개선한다.
- 존재하지 않는 경험이나 사실을 만들어내지 않는다.
- 답변에서 실제로 반복되는 말버릇/추임새가 있을 때만 fillerWords에 포함한다. 없으면 빈 배열로 응답한다.
- overallScore와 categoryScores는 서로 일관되게 산정한다 — 세부 항목들의 전반적인 수준이 overallScore에 반영되어야 한다.
- 같은 연습 유형({category})에는 항상 동일한 평가 기준을 적용하고, 실제 내용 수준이 비슷한 답변에는 유사한 점수를 부여한다. 단순히 표현이나 단어 선택만 달라졌다는 이유로 점수를 임의로 올리거나 내리지 않는다.
- 한국어로 작성한다.
- 반드시 아래 JSON 형식으로만 응답한다 (다른 텍스트 없이, 마크다운 코드블록 없이):

{{
  "overallScore": 82,
  "categoryScores": {{{score_example}}},
  "summary": "핵심 의견은 명확하지만 구체적인 사례가 부족합니다.",
  "strengths": [
    {{"title": "주장이 명확합니다", "description": "질문에 대한 입장을 처음에 명확하게 제시했습니다."}}
  ],
  "improvements": [
    {{"title": "구체적인 사례를 추가하세요", "description": "주장과 이유는 있지만 실제 경험이나 사례가 없어 설득력이 약합니다.", "priority": "high"}}
  ],
  "betterAnswer": "개선된 답변 전체 텍스트",
  "fillerWords": [
    {{"word": "음", "count": 4}}
  ],
  "nextPractice": "같은 질문에 자신의 실제 경험을 하나 포함해서 다시 답해보세요."
}}
"""

    client = _make_client()
    response = await client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_build_response_schema(score_fields),
            temperature=0.3,
        ),
    )
    data = json.loads(response.text)

    overall_score = max(0, min(100, int(data.get("overallScore", 0) or 0)))

    raw_category_scores = data.get("categoryScores")
    if not isinstance(raw_category_scores, dict):
        raw_category_scores = {}
    category_scores: dict[str, int] = {}
    for key, _ in score_fields:
        value = raw_category_scores.get(key)
        try:
            value = int(value)
        except (TypeError, ValueError):
            # Gemini가 스키마를 어기고 항목을 누락한 경우에만 발생 — 근거 없이
            # 새 점수를 만들지 않고 overallScore로 대체한다.
            value = overall_score
        category_scores[key] = max(0, min(100, value))

    strengths = [
        {
            "title": str(s.get("title", "")).strip(),
            "description": str(s.get("description", "")).strip(),
        }
        for s in data.get("strengths", [])
        if isinstance(s, dict)
    ]
    improvements = [
        {
            "title": str(i.get("title", "")).strip(),
            "description": str(i.get("description", "")).strip(),
            "priority": str(i.get("priority", "medium")).strip() or "medium",
        }
        for i in data.get("improvements", [])
        if isinstance(i, dict)
    ][:3]
    filler_words = [
        {"word": str(f.get("word", "")).strip(), "count": int(f.get("count", 0))}
        for f in data.get("fillerWords", [])
        if isinstance(f, dict) and str(f.get("word", "")).strip()
    ]

    return {
        "overallScore": overall_score,
        "categoryScores": category_scores,
        "summary": str(data.get("summary", "")).strip(),
        "strengths": strengths,
        "improvements": improvements,
        "betterAnswer": str(data.get("betterAnswer", "")).strip(),
        "fillerWords": filler_words,
        "nextPractice": str(data.get("nextPractice", "")).strip(),
        "speechAnalysis": _compute_speech_analysis(transcript, duration_seconds),
    }
