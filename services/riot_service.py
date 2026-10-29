import logging
import os

import httpx

logger = logging.getLogger(__name__)

# asia: 계정/PUUID 조회
_ASIA_BASE = "https://asia.api.riotgames.com"
# kr: 소환사 정보 조회
_KR_BASE = "https://kr.api.riotgames.com"


def _headers() -> dict:
    key = os.getenv("RIOT_API_KEY", "")
    if not key:
        raise ValueError("RIOT_API_KEY not set")
    return {"X-Riot-Token": key, "Accept-Language": "ko-KR,ko;q=0.9"}


async def get_puuid_by_riot_id(game_name: str, tag_line: str) -> dict:
    """
    GameName + TagLine → PUUID 조회
    Returns: {puuid, gameName, tagLine}
    """
    url = f"{_ASIA_BASE}/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=_headers())
        if resp.status_code == 404:
            raise ValueError(f"존재하지 않는 라이엇 ID: {game_name}#{tag_line}")
        if resp.status_code == 403:
            raise ValueError("Riot API 키가 만료되었습니다")
        resp.raise_for_status()
        data = resp.json()

    return {
        "puuid": data.get("puuid", ""),
        "game_name": data.get("gameName", game_name),
        "tag_line": data.get("tagLine", tag_line),
    }


async def get_summoner_by_puuid(puuid: str) -> dict:
    """
    PUUID → 소환사 정보 (프로필 아이콘 ID, 레벨)
    Returns: {profileIconId, summonerLevel, name}
    """
    url = f"{_KR_BASE}/lol/summoner/v4/summoners/by-puuid/{puuid}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=_headers())
        if resp.status_code == 404:
            raise ValueError("소환사 정보를 찾을 수 없습니다")
        resp.raise_for_status()
        data = resp.json()

    return {
        "profile_icon_id": data.get("profileIconId", 29),
        "summoner_level": data.get("summonerLevel", 0),
        "summoner_name": data.get("name", ""),
    }
