import logging

import httpx
from services.epl_team_names import en_team

logger = logging.getLogger(__name__)

NAVER_SPORTS_BASE_URL = "https://api-gw.sports.naver.com"
NAVER_SPORTS_SEASONS_URL = f"{NAVER_SPORTS_BASE_URL}/statistics/categories/epl/seasons"
NAVER_SPORTS_TEAM_STATS_URL = f"{NAVER_SPORTS_BASE_URL}/statistics/categories/epl/seasons/{{season_code}}/teams"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://m.sports.naver.com/wfootball/record/index?category=epl",
}


async def _fetch_current_season_code(client: httpx.AsyncClient) -> str:
    """현재 시즌의 seasonCode를 조회합니다 (연도가 아닌 불투명 토큰)."""
    resp = await client.get(NAVER_SPORTS_SEASONS_URL)
    resp.raise_for_status()
    data = resp.json()
    seasons = data.get("result", {}).get("seasons", [])
    if not seasons:
        raise RuntimeError("No EPL seasons returned from Naver Sports API")

    current = next((s for s in seasons if s.get("isSeason") == "Y"), None)
    if current is None:
        # isSeason 플래그가 없으면 연도가 가장 큰(가장 최신) 시즌으로 대체
        current = max(seasons, key=lambda s: s.get("year", 0))

    season_code = current.get("seasonCode", "")
    if not season_code:
        raise RuntimeError("EPL season entry missing seasonCode")
    return season_code


async def fetch_epl_standings() -> list[dict]:
    """EPL 순위표를 Naver Sports API에서 가져옵니다."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            season_code = await _fetch_current_season_code(client)
            resp = await client.get(
                NAVER_SPORTS_TEAM_STATS_URL.format(season_code=season_code)
            )
            resp.raise_for_status()
            data = resp.json()
        return _parse_standings(data)
    except httpx.HTTPError as e:
        logger.error("EPL standings fetch failed: %s", e)
        raise RuntimeError(f"Failed to fetch EPL standings: {e}")


def _parse_standings(data: dict) -> list[dict]:
    rows = data.get("result", {}).get("seasonTeamStats", []) or []

    standings = []
    for row in rows:
        team_name = en_team(row.get("teamName", ""))
        won = row.get("wins", 0)
        drawn = row.get("draws", 0)
        lost = row.get("losses", 0)

        standings.append({
            "rank": row.get("rank", 0),
            "team": team_name,
            "emblem": row.get("teamEmblemUrl", ""),
            "played": row.get("matchesPlayed", 0),
            "won": won,
            "drawn": drawn,
            "lost": lost,
            "goals_for": row.get("goals", 0),
            "goals_against": row.get("goalsConceded", 0),
            "goal_difference": row.get("goalsDifference", 0),
            "points": row.get("points", won * 3 + drawn),
            "form": row.get("lastFiveGames", ""),
        })

    standings.sort(key=lambda x: x["rank"])

    logger.info("Parsed %d EPL standings entries", len(standings))
    return standings
