import logging

import httpx

from .lck_schedule import (
    _API_KEY,
    _HEADERS,
    _LCK_LEAGUE_ID,
    _LOL_ESPORTS_URL,
    _to_kst,
)

logger = logging.getLogger(__name__)

_TEAMS_URL = "https://esports-api.lolesports.com/persisted/gw/getTeams"

_ROLE_MAP = {
    "top": "탑",
    "jungle": "정글",
    "mid": "미드",
    "bottom": "원딜",
    "support": "서폿",
}
_ROLE_ORDER = ["top", "jungle", "mid", "bottom", "support"]


async def fetch_lck_team_detail(team_code: str) -> dict:
    """팀 코드로 로스터 + 최근 완료 경기 5개 반환."""
    params = {"hl": "ko-KR", "leagueId": _LCK_LEAGUE_ID}

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        # ── 1. 스케줄 전체 수집 ──────────────────────────────────────
        try:
            resp = await client.get(_LOL_ESPORTS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("Schedule fetch failed: %s", e)
            raise RuntimeError(f"일정 조회 실패: {e}")

        schedule = data.get("data", {}).get("schedule", {})
        all_events: list[dict] = list(schedule.get("events", []))

        older_token = schedule.get("pages", {}).get("older")
        if older_token:
            try:
                resp2 = await client.get(
                    _LOL_ESPORTS_URL, params={**params, "pageToken": older_token}
                )
                resp2.raise_for_status()
                all_events.extend(
                    resp2.json()
                    .get("data", {})
                    .get("schedule", {})
                    .get("events", [])
                )
            except httpx.HTTPError:
                pass

        # ── 2. 완료 경기 수집 ───────────────────────────────────────
        code_upper = team_code.upper()
        completed: list[dict] = []

        for ev in all_events:
            if ev.get("type") != "match":
                continue
            match = ev.get("match", {})
            teams = match.get("teams", [])
            if len(teams) < 2:
                continue

            codes = {t.get("code", "").upper() for t in teams}
            if code_upper not in codes:
                continue

            if ev.get("state") != "completed":
                continue

            dt_kst = _to_kst(ev.get("startTime", ""))
            if dt_kst is None:
                continue

            home, away = teams[0], teams[1]
            home_wins = home.get("result", {}).get("gameWins", 0)
            away_wins = away.get("result", {}).get("gameWins", 0)

            completed.append(
                {
                    "game_id": match.get("id", ""),
                    "home_team": home.get("name", ""),
                    "away_team": away.get("name", ""),
                    "home_code": home.get("code", ""),
                    "away_code": away.get("code", ""),
                    "home_emblem": home.get("image", ""),
                    "away_emblem": away.get("image", ""),
                    "home_score": home_wins,
                    "away_score": away_wins,
                    "score": f"{home_wins}:{away_wins}",
                    "status": "경기 종료",
                    "time": dt_kst.strftime("%H:%M"),
                    "date": dt_kst.date().isoformat(),
                    "stadium": "LoL Park",
                    "home_record": home.get("record"),
                    "away_record": away.get("record"),
                }
            )

        recent_matches = completed[-5:]

        # ── 3. 로스터 조회 (팀 코드로 직접 검색) ────────────────────
        roster: list[dict] = []
        try:
            r = await client.get(_TEAMS_URL, params={"hl": "ko-KR"})
            r.raise_for_status()
            all_teams: list[dict] = r.json().get("data", {}).get("teams", [])
            logger.info("getTeams returned %d teams total", len(all_teams))

            # LCK 소속 팀 우선, 없으면 코드만 일치하는 팀
            # homeLeague가 null인 팀 있으므로 `or {}` 로 안전하게 처리
            team_obj = next(
                (t for t in all_teams
                 if t.get("code", "").upper() == code_upper
                 and (t.get("homeLeague") or {}).get("name") == "LCK"),
                None,
            ) or next(
                (t for t in all_teams if t.get("code", "").upper() == code_upper),
                None,
            )
            if team_obj:
                players: list[dict] = team_obj.get("players", [])
                logger.info("Found %s roster: %d players", code_upper, len(players))
                players.sort(
                    key=lambda p: (
                        _ROLE_ORDER.index(p.get("role", ""))
                        if p.get("role") in _ROLE_ORDER
                        else 99
                    )
                )
                roster = [
                    {
                        "summoner_name": p.get("summonerName", ""),
                        "role": p.get("role", ""),
                        "role_kr": _ROLE_MAP.get(p.get("role", ""), p.get("role", "")),
                        "first_name": p.get("firstName", ""),
                        "last_name": p.get("lastName", ""),
                        "image": p.get("image", ""),
                    }
                    for p in players
                    if p.get("role") in _ROLE_ORDER  # 포지션 있는 선수만
                ]
            else:
                logger.warning("Team code %s not found in getTeams response", code_upper)
        except httpx.HTTPError as e:
            logger.warning("Roster fetch failed for %s: %s", team_code, e)

        return {
            "team_code": team_code,
            "roster": roster,
            "recent_matches": recent_matches,
        }


async def fetch_lck_lineup(home_code: str, away_code: str) -> dict:
    """두 팀 선발 로스터 반환 (getTeams 단일 호출)."""
    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        try:
            r = await client.get(_TEAMS_URL, params={"hl": "ko-KR"})
            r.raise_for_status()
            all_teams: list[dict] = r.json().get("data", {}).get("teams", [])
        except httpx.HTTPError as e:
            raise RuntimeError(f"팀 정보 조회 실패: {e}")

    def get_roster(code: str) -> list[dict]:
        code_upper = code.upper()
        team_obj = next(
            (t for t in all_teams if t.get("code", "").upper() == code_upper),
            None,
        )
        if not team_obj:
            return []
        players = team_obj.get("players", [])
        players.sort(
            key=lambda p: (
                _ROLE_ORDER.index(p.get("role", ""))
                if p.get("role") in _ROLE_ORDER
                else 99
            )
        )
        return [
            {
                "summoner_name": p.get("summonerName", ""),
                "role": p.get("role", ""),
                "role_kr": _ROLE_MAP.get(p.get("role", ""), p.get("role", "")),
                "first_name": p.get("firstName", ""),
                "last_name": p.get("lastName", ""),
                "image": p.get("image", ""),
            }
            for p in players
            if p.get("role") in _ROLE_ORDER
        ]

    return {
        "home": get_roster(home_code),
        "away": get_roster(away_code),
    }
