import json
import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_ARTWORK_MODEL = "gemini-2.5-flash"

_TYPE_LABELS: dict[str, str] = {
    "parking":    "주차 정보 (혼잡도, 만차 여부, 대안 교통수단)",
    "restaurant": "맛집 정보 (추천 메뉴, 웨이팅, 특징)",
    "food":       "먹거리 정보 (메뉴, 가격, 인기 품목)",
    "traffic":    "교통 정보 (대중교통, 혼잡 구간, 소요 시간)",
    "notice":     "공지 정보 (변경사항, 임시 운영, 주의사항)",
    "tip":        "관람 팁 (추천 좌석, 입장 팁, 편의시설)",
}

_SYSTEM_PROMPT = """당신은 사진을 기반으로 대상의 상태를 정밀하게 진단하는 지능형 상태 분석 전문가입니다.
사용자가 제공한 이미지를 바탕으로 객관적이고 데이터 중심적인 분석 결과를 제공합니다.

주요 작업:
1. 객체 식별 및 분류: 이미지 내 주요 대상을 탐지하고 그 종류를 분류합니다.
2. 상태 수치화 (Scoring): 대상의 상태를 0.0(최악)에서 1.0(최상) 사이의 수치로 평가합니다.
3. 상태 특징 추출: 색상, 질감, 수량 등 시각적 특징을 명확히 설명합니다.
4. 맞춤형 추천 조치: 분석된 상태를 바탕으로 구체적인 행동이나 해결책을 제안합니다.

제약 조건:
- 이미지 내 사람의 얼굴이나 민감한 개인정보는 식별하지 마십시오.
- 사진이 지나치게 흐릿하거나 식별 불가능한 경우 분석하지 마십시오.

반드시 다음 JSON 형식으로만 응답하십시오 (다른 텍스트 없이):
{
  "analysis_result": {
    "target_name": "분석 대상 이름",
    "state_score": 0.85,
    "summary": "핵심 상태를 한 문장으로 요약 (감정적이고 직관적인 표현)",
    "one_liner_dis": "날카롭고 위트있는 한 줄 평가. 살짝 찌르되 기분 나쁘지 않게 비꼬기. 예: '역시 완벽하면 정 없지. 저 침대의 역동적인 주름이 이 방의 유일한 예술적 포인트라고 봐' / '미니멀리즘의 정석이네. 너무 정갈해서 숨소리도 조심해야 할 것 같아. 혹시 인테리어 컨셉이 '수도원'이야?'",
    "key_characteristics": ["특징 1", "특징 2", "특징 3", "특징 4"],
    "icon_suggestions": ["sun", "playground", "safety", "clean"],
    "detailed_description": "상태에 대한 상세 서술",
    "recommendations": ["추천 조치 1", "추천 조치 2"],
    "recommendation_scores": [3, 2],
    "status_color_code": "#HEX색상코드"
  }
}

summary: 분석 결과를 한 문장으로 요약. 위트있고 감성적인 표현 사용. 예: '여기서 자면 알람 필요 없겠는데요', '침대가 사람을 유혹하는 수준입니다'
one_liner_dis: 기분 안 나쁘게 비꼬기, 위트 한 스푼 섞어서 뼈만 살짝 때려볼게요. 상대방이 웃으면서도 "어라, 이거 나 까는 건가?" 싶게 만드는 게 포인트입니다.
recommendation_scores: 각 추천 조치를 실행했을 때 예상 점수 상승폭 (1~5 사이 정수). recommendations와 개수 동일.
icon_suggestions: key_characteristics 각 항목에 대응하는 Material icon 키워드 (영어).
  가능한 키워드: sun, cloud, rain, umbrella, water, tree, park, playground, safety, clean, warning, check, star, heart, fire, snow, wind, leaf, flower, bug, pet, food, medicine, tool, home, car, building, people, baby, sport, music, book, camera, phone, clock, calendar, map, flag, lock, key, light, color, paint, brush, eye, hand, foot, smile, sad, angry, question, info

status_color_code 기준:
- 0.8~1.0: #4CAF50 (초록 - 양호)
- 0.5~0.79: #FF9800 (주황 - 주의)
- 0.0~0.49: #F44336 (빨강 - 위험)
"""


def _make_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured on server")
    return genai.Client(api_key=api_key)


async def _download_image(url: str) -> tuple[bytes, str]:
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return r.content, mime


async def _artwork_title_prompt(image_bytes: bytes, mime_type: str, mood: str) -> str:
    client = _make_client()
    prompt = f"""이 그림 작품을 보고 어울리는 제목 3가지를 제안해주세요.
작가의 현재 감정: {mood}
조건: 시적이고 감성적인 한국어 제목, 각 제목 2~8글자
JSON으로만 응답: {{"titles": ["제목1", "제목2", "제목3"]}}"""
    response = await client.aio.models.generate_content(
        model=_ARTWORK_MODEL,
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.8),
    )
    return response.text


async def _artwork_poem_prompt(image_bytes: bytes, mime_type: str, mood: str, title: str) -> str:
    client = _make_client()
    prompt = f"""이 그림 작품에 어울리는 짧은 시를 써주세요.
제목: {title} / 감정: {mood}
조건: 3~5줄, 서정적 표현, 줄바꿈은 \\n으로
JSON으로만 응답: {{"poem": "시 내용"}}"""
    response = await client.aio.models.generate_content(
        model=_ARTWORK_MODEL,
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.85),
    )
    return response.text


async def _artwork_analysis_prompt(image_bytes: bytes, mime_type: str, title: str, mood: str) -> str:
    client = _make_client()
    prompt = f"""이 그림 작품에 대한 따뜻한 감상문을 써주세요.
제목: {title} / 감정: {mood}
조건: 100~150자, 색감·구도·분위기 언급, 작가에게 영감을 주는 표현
JSON으로만 응답: {{"analysis": "감상문 내용"}}"""
    response = await client.aio.models.generate_content(
        model=_ARTWORK_MODEL,
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.7),
    )
    return response.text


async def generate_artwork_title_from_bytes(image_bytes: bytes, mime_type: str, mood: str) -> str:
    return await _artwork_title_prompt(image_bytes, mime_type, mood)


async def generate_artwork_poem_from_bytes(image_bytes: bytes, mime_type: str, mood: str, title: str) -> str:
    return await _artwork_poem_prompt(image_bytes, mime_type, mood, title)


async def generate_artwork_analysis_from_bytes(image_bytes: bytes, mime_type: str, title: str, mood: str) -> str:
    return await _artwork_analysis_prompt(image_bytes, mime_type, title, mood)


async def generate_artwork_title(image_url: str, mood: str) -> str:
    image_bytes, mime_type = await _download_image(image_url)
    return await _artwork_title_prompt(image_bytes, mime_type, mood)


async def generate_artwork_poem(image_url: str, mood: str, title: str) -> str:
    image_bytes, mime_type = await _download_image(image_url)
    return await _artwork_poem_prompt(image_bytes, mime_type, mood, title)


async def generate_artwork_analysis(image_url: str, title: str, mood: str) -> str:
    image_bytes, mime_type = await _download_image(image_url)
    return await _artwork_analysis_prompt(image_bytes, mime_type, title, mood)


async def analyze_image(image_bytes: bytes, mime_type: str) -> str:
    client = _make_client()
    response = await client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            "이 이미지의 대상 상태를 분석하여 JSON으로 응답해주세요.",
        ],
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return response.text


async def summarize_update(stadium: str, update_type: str, raw_text: str) -> str | None:
    """raw_text에서 type별 핵심 정보 추출 → 앱 등록용 요약 문장 반환. 실패 시 None."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or not raw_text.strip():
        return None

    type_label = _TYPE_LABELS.get(update_type, update_type)

    prompt = (
        f"아래는 '{stadium}' 관련 블로그/커뮤니티에서 수집한 텍스트입니다.\n\n"
        f"[수집 텍스트]\n{raw_text}\n\n"
        f"이 텍스트에서 [{type_label}]에 해당하는 핵심 정보만 추출해 요약하세요.\n\n"
        "조건:\n"
        "- 구체적 수치/사실 중심 (예: '경기 시작 2시간 전 만차', '버스 502번 직결')\n"
        "- 불확실하거나 광고성 내용 제외\n"
        "- 자연스러운 한국어, 50자 이내\n"
        "- 관련 정보가 전혀 없으면 message를 빈 문자열로\n\n"
        'JSON으로만 응답: {"message": "요약 내용"}'
    )

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        data = json.loads(response.text)
        msg = data.get("message", "").strip()
        return msg if msg else None
    except Exception as e:
        logger.debug("summarize_update failed for %s/%s: %s", stadium, update_type, e)
        return None


_RECIPE_SUMMARY_LANG: dict[str, str] = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "vi": "Vietnamese",
    "th": "Thai",
}


async def summarize_recipe(recipe_name: str, steps: str, locale: str = "ko") -> str:
    """한식 레시피 AI 요약 — JSON 문자열 반환. locale: ko/en/ja/vi/th."""
    client = _make_client()

    lang_code = locale.split("_")[0].split("-")[0]
    language = _RECIPE_SUMMARY_LANG.get(lang_code, "Korean")

    prompt = f"""Analyze the following Korean recipe and respond in JSON only (no markdown fences).
All text VALUES in the JSON (summary, tips, ingredient, substitute, note) must be written in {language}. Keep the JSON keys in English exactly as shown below.

Recipe: {recipe_name}
Cooking steps: {steps}

Response format:
{{
  "summary": "3-line summary (each line separated by \\n)",
  "tips": "2 common failure points, numbered on separate lines. e.g. 1. ...\\n2. ...",
  "alternatives": [
    {{
      "ingredient": "original ingredient name",
      "substitute": "alternative ingredient name",
      "similarity": "Very Similar",
      "note": "one sentence on flavor or texture difference"
    }}
  ]
}}

similarity must be exactly one of: "Very Similar", "Similar", "Acceptable" (keep these three values in English regardless of {language}). Include 1-2 alternatives maximum."""

    response = await client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    return response.text


async def get_ingredient_substitute(ingredient: str, locale: str) -> str:
    """한식 재료 대체재 추천 — JSON 문자열 반환."""
    client = _make_client()
    lang_code = locale.split("_")[0].split("-")[0]
    language = _RECIPE_SUMMARY_LANG.get(lang_code, "Korean")

    prompt = f"""Respond in {language}. Suggest a substitute ingredient for the Korean ingredient "{ingredient}".
Respond in JSON only (values in {language}, keys in English exactly as shown):
{{
  "original": "{ingredient}",
  "substitute": "substitute ingredient name",
  "ratio": "ratio/amount (e.g. 1:1)",
  "note": "one line on flavor difference or caution"
}}"""
    response = await client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    return response.text


async def translate_titles(titles: list[str], locale: str) -> list[str]:
    """유튜브 영상 제목 일괄 번역 (K-Food) — 실패 시 원문 그대로 반환.

    한 번의 호출로 여러 제목을 같이 번역해 Gemini 호출 횟수를 아낀다.
    원문 자체가 어느 언어인지는 신경 쓰지 않는다 — 대상 언어로만 옮기면 된다.
    """
    if not titles:
        return []
    lang_code = locale.split("_")[0].split("-")[0]
    language = _RECIPE_SUMMARY_LANG.get(lang_code)
    if not language or lang_code == "ko":
        return titles

    client = _make_client()
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    prompt = f"""Translate the following YouTube video titles (Korean home-cooking videos) into natural, casual {language} suitable for a recipe app. Keep them short like real video titles — do not explain or add quotes.

{numbered}

Respond in JSON only: {{"translations": ["translated 1", "translated 2", ...]}}
The array must have exactly {len(titles)} items, in the same order as the input."""

    response = await client.aio.models.generate_content(
        model=_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    data = json.loads(response.text)
    translations = data.get("translations", [])
    if not isinstance(translations, list) or len(translations) != len(titles):
        return titles
    return [str(t).strip() or orig for t, orig in zip(translations, titles)]


async def generate_game_insight(
    home_team: str,
    away_team: str,
    home_pitcher: str,
    home_era: str,
    home_record: str,
    away_pitcher: str,
    away_era: str,
    away_record: str,
) -> dict:
    """KBO 선발 매치업 AI 인사이트 — 관전 포인트 3개 + 요약 배지. 실패 시 빈 결과."""
    empty: dict = {"insights": [], "summary": ""}
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return empty

    prompt = (
        f"KBO 오늘 경기 데이터:\n"
        f"원정 {away_team}: 선발 {away_pitcher or '미정'}, ERA {away_era or 'N/A'}, 성적 {away_record or 'N/A'}\n"
        f"홈 {home_team}: 선발 {home_pitcher or '미정'}, ERA {home_era or 'N/A'}, 성적 {home_record or 'N/A'}\n\n"
        "너는 야구에 미친 10년 차 도파민 중독 야구 커뮤니티 에디터다. "
        "주어진 선발 매치업과 경기 데이터를 바탕으로 야구 팬들이 흥미를 느낄 만한 "
        "'오늘의 관전 포인트'를 3줄로 작성하라.\n\n"
        "[말투]\n"
        "존댓말을 쓰되, 뻔한 신문 기사체 금지. 커뮤니티 느낌의 찰진 입담, 위트, 힙한 톤앤매너 유지.\n\n"
        "[내용 구성 — 반드시 이 순서와 역할을 지켜서 딱 3줄]\n"
        "1번째 줄: 양 팀 선발 투수의 기록/최근 기세 비교 또는 팩트 폭격 (상대적 우위 강조)\n"
        "2번째 줄: 이번 매치업의 핵심 분수령 (타선 vs 투수, 천적 관계, 최근 상대 전적 등)\n"
        "3번째 줄: 팬들의 도파민을 자극하는 매콤한 한 줄 질문/전망\n\n"
        "[지침]\n"
        "- '선수가 어떤 투구를 보여줄지가 핵심입니다' 같은 영혼 없는 소리 절대 금지\n"
        "- ERA, 승패 등 숫자 데이터를 적극 활용해 직관적으로 비교할 것\n"
        "- 선발 투수 한 명한테만 편중되지 말고, 양 팀 매치업의 구도를 살릴 것\n"
        "- 25~45자, 한 문장, 한국어\n"
        "- 클릭베이트/뉴스 헤드라인 제목체 금지 (예: '~할까?', '~vs~')\n"
        "- 데이터가 없는 항목(선발 미정, ERA N/A 등)은 억지로 언급하지 말 것\n"
        "- 문장 맨 앞에 어울리는 이모지 1개만 붙일 것 (번호 없이)\n\n"
        "마지막으로 위 3줄을 종합한 한줄 요약 배지를 만드세요 "
        "(예: '🏆 투수전 가능성 높음', '💥 타격전 예상', '🔥 접전 예상', '⚾ 홈팀 근소 우세' 같은 스타일, "
        "이모지 1개 + 8자 이내 짧은 표현).\n\n"
        'JSON으로만 응답: {"insights": ["이모지 1번째줄", "이모지 2번째줄", "이모지 3번째줄"], "summary": "이모지 요약"}'
    )

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7,
            ),
        )
        data = json.loads(response.text)
        insights = [str(i).strip() for i in data.get("insights", []) if str(i).strip()][:3]
        summary = str(data.get("summary", "")).strip()
        return {"insights": insights, "summary": summary}
    except Exception as e:
        logger.debug("generate_game_insight failed: %s", e)
        return empty


async def summarize_community(posts: list[str]) -> list[str]:
    """
    커뮤니티 인기글 제목 목록 → MZ 말투 야구 팬 반응 3줄 요약.
    실패 시 빈 리스트 반환.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or not posts:
        return []

    numbered = "\n".join(f"{i+1}. {p}" for i, p in enumerate(posts[:25]))
    prompt = (
        "아래는 오늘 한국 야구(KBO) 커뮤니티의 실시간 인기글 제목입니다.\n\n"
        f"[인기글 목록]\n{numbered}\n\n"
        "다음 조건으로 오늘 야구 팬 반응 3줄 요약을 작성해주세요:\n"
        "- 비속어, 정치적 발언 완전 제외\n"
        "- MZ세대 야구 팬 말투 (예: '~각', '~임', '진짜', '레전드' 등)\n"
        "- 유머러스하고 위트 있게, 팬 감성 살려서\n"
        "- 각 줄 40자 이내\n"
        "- 번호 붙이지 말고 텍스트만\n\n"
        'JSON으로만 응답: {"lines": ["줄1", "줄2", "줄3"]}'
    )

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.8,
            ),
        )
        data = json.loads(response.text)
        lines = data.get("lines", [])
        return [str(l).strip() for l in lines if str(l).strip()][:3]
    except Exception as e:
        logger.error("summarize_community failed: %s", e)
        return []


async def analyze_bible_topics(query: str) -> dict:
    """Bible Hub — 검색어(성경 구절 또는 주제)를 분석해 핵심 주제와 관련 구절을 반환.

    실패 시 topics/related_verses 모두 빈 리스트.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return {"topics": [], "related_verses": []}

    prompt = f"""다음은 성경 콘텐츠 검색 허브 앱에서 사용자가 입력한 검색어입니다: "{query}"

이 검색어가 성경 구절(예: "창세기 1장", "요한복음 3:16")이든 신앙 주제(예: "불안", "감사")이든 관계없이 분석해서 JSON으로만 응답하세요 (마크다운 코드블록 없이):

{{
  "topics": ["핵심 주제 키워드 1", "키워드 2", "키워드 3", "키워드 4"],
  "related_verses": [
    {{"reference": "시편 19편", "reason": "이 구절과 관련된 이유를 한 문장으로"}},
    {{"reference": "요한복음 1장", "reason": "..."}}
  ]
}}

- topics: 3~5개, 짧은 명사형 키워드 (예: 창조, 빛, 질서, 하나님의 능력)
- related_verses: 2~4개, 실제로 존재하는 성경 구절 참조만 사용. reference는 "책 이름 장" 또는 "책 이름 장:절" 형식.
"""

    client = _make_client()
    try:
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.4,
            ),
        )
        data = json.loads(response.text)
        topics = [str(t).strip() for t in data.get("topics", []) if str(t).strip()][:5]
        related_verses = [
            {
                "reference": str(v.get("reference", "")).strip(),
                "reason": str(v.get("reason", "")).strip(),
            }
            for v in data.get("related_verses", [])
            if str(v.get("reference", "")).strip()
        ][:4]
        return {"topics": topics, "related_verses": related_verses}
    except Exception as e:
        logger.error("analyze_bible_topics failed: %s", e)
        return {"topics": [], "related_verses": []}
