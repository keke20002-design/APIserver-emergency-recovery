import json
import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"


def _get_client() -> genai.Client:
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))


async def generate_worldcup_match_insight(
    home_team: str,
    away_team: str,
    group: str = "",
    round_name: str = "",
) -> list[str]:
    """FIFA 월드컵 경기 관전 포인트 3개 생성.

    반환: ["🔥 포인트1", "⚡ 포인트2", "🏆 포인트3"]
    실패 시 빈 리스트 반환.
    """
    context_parts = []
    if group:
        context_parts.append(f"조: {group}조")
    if round_name and round_name != "조별리그":
        context_parts.append(f"라운드: {round_name}")
    context_str = ", ".join(context_parts)

    prompt = f"""FIFA 월드컵 2026 경기: {home_team} vs {away_team}
{context_str}

축구 팬이 흥미를 느낄 관전 포인트 3가지를 작성하세요.
조건:
- 각 항목은 25자 이내
- 이모지(🔥, ⚡, 🏆, 🎯, 💥 중 하나)로 시작
- 자연스러운 한국어
- 번호·기호 없이

JSON으로만 응답: {{"points": ["포인트1", "포인트2", "포인트3"]}}"""

    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.8,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        parsed = json.loads(text)
        points = parsed.get("points", [])
        if isinstance(points, list) and points:
            return points[:3]
        return []
    except Exception as e:
        logger.warning("generate_worldcup_match_insight failed (%s vs %s): %s", home_team, away_team, e)
        return []


async def generate_worldcup_win_prediction(
    home_team: str,
    away_team: str,
) -> dict:
    """FIFA 월드컵 경기 승부 예측 %.

    반환: {"home": 55, "draw": 22, "away": 23, "home_team": "...", "away_team": "..."}
    실패 시 균등 분배(33/34/33) 반환.
    """
    prompt = f"""FIFA 월드컵 2026 경기: {home_team}(홈) vs {away_team}(원정)

두 팀의 최근 폼, 역대 전적, 선수 수준을 종합적으로 분석해
승부 예측 확률을 작성하세요.

조건:
- home + draw + away의 합계는 반드시 100
- 각 값은 정수

JSON으로만 응답: {{"home": 숫자, "draw": 숫자, "away": 숫자}}"""

    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.5,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        parsed = json.loads(text)
        home_pct = int(parsed.get("home", 33))
        draw_pct = int(parsed.get("draw", 34))
        away_pct = int(parsed.get("away", 33))

        # 합계 보정
        total = home_pct + draw_pct + away_pct
        if total != 100:
            diff = 100 - total
            home_pct += diff

        return {
            "home_team": home_team,
            "away_team": away_team,
            "home": home_pct,
            "draw": draw_pct,
            "away": away_pct,
        }
    except Exception as e:
        logger.warning("generate_worldcup_win_prediction failed (%s vs %s): %s", home_team, away_team, e)
        return {
            "home_team": home_team,
            "away_team": away_team,
            "home": 33,
            "draw": 34,
            "away": 33,
        }
