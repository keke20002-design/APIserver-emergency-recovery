import logging
import os
from datetime import date, datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

_LOL_ESPORTS_URL = "https://esports-api.lolesports.com/persisted/gw/getSchedule"
_LOL_LIVE_URL = "https://esports-api.lolesports.com/persisted/gw/getLive"
_LCK_LEAGUE_ID = "98767991310872058"
_API_KEY = os.getenv("LOL_ESPORTS_API_KEY", "")
_KST = timezone(timedelta(hours=9))

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "x-api-key": _API_KEY,
}

_STATUS_MAP = {
    "unstarted": "경기 전",
    "inProgress": "경기 중",
    "completed": "경기 종료",
}


def _to_kst(utc_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(_KST)
    except (ValueError, AttributeError):
        return None


def _parse_event(ev: dict) -> dict | None:
    """단일 이벤트를 game dict로 변환. 유효하지 않으면 None 반환."""
    if ev.get("type") != "match":
        return None
    dt_kst = _to_kst(ev.get("startTime", ""))
    if dt_kst is None:
        return None

    match = ev.get("match", {})
    teams = match.get("teams", [])
    if len(teams) < 2:
        return None

    home, away = teams[0], teams[1]
    state = ev.get("state", "unstarted")
    status = _STATUS_MAP.get(state, state)

    home_wins = (home.get("result") or {}).get("gameWins")
    away_wins = (away.get("result") or {}).get("gameWins")
    score = None
    if state in ("inProgress", "completed") and home_wins is not None:
        score = f"{away_wins}:{home_wins}"

    return {
        "game_id": match.get("id", ""),
        "home_team": home.get("name", ""),
        "away_team": away.get("name", ""),
        "home_code": home.get("code", ""),
        "away_code": away.get("code", ""),
        "home_emblem": home.get("image", ""),
        "away_emblem": away.get("image", ""),
        "stadium": "LoL Park",
        "time": dt_kst.strftime("%H:%M"),
        "status": status,
        "score": score,
        "home_score": home_wins if state in ("inProgress", "completed") else None,
        "away_score": away_wins if state in ("inProgress", "completed") else None,
        "date": dt_kst.date().isoformat(),
        "block_name": ev.get("blockName", ""),
        "home_record": home.get("record"),
        "away_record": away.get("record"),
    }


def _parse_events(events: list, target_date: date) -> list[dict]:
    games = []
    for ev in events:
        game = _parse_event(ev)
        if game and date.fromisoformat(game["date"]) == target_date:
            games.append(game)
    return games


async def _fetch_all_events(target_date: date | None = None) -> list[dict]:
    """현재 페이지부터 시작해 target_date가 포함될 때까지 older 페이지를 순차 탐색."""
    params = {"hl": "ko-KR", "leagueId": _LCK_LEAGUE_ID}
    all_events: list[dict] = []

    # 과거 날짜 조회 시 더 많은 페이지 탐색, 그 외에는 1페이지로 제한
    is_past = target_date is not None and target_date < date.today()
    max_older = 8 if is_past else 1

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        try:
            resp = await client.get(_LOL_ESPORTS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("LCK schedule fetch failed: %s", e)
            raise RuntimeError(f"LCK 일정 조회 실패: {e}")

        schedule = (data.get("data") or {}).get("schedule") or {}
        all_events.extend(schedule.get("events", []))
        older_token = schedule.get("pages", {}).get("older")
        fetched = 0

        while older_token and fetched < max_older:
            try:
                resp = await client.get(
                    _LOL_ESPORTS_URL,
                    params={**params, "pageToken": older_token},
                )
                resp.raise_for_status()
                page = (resp.json().get("data") or {}).get("schedule") or {}
                page_events = page.get("events", [])
                all_events.extend(page_events)
                older_token = page.get("pages", {}).get("older")
                fetched += 1

                # target_date 이전 데이터가 확보되면 중단
                if target_date and page_events:
                    page_dates = [
                        _to_kst(ev.get("startTime", "")).date()
                        for ev in page_events
                        if _to_kst(ev.get("startTime", ""))
                    ]
                    if page_dates and min(page_dates) <= target_date:
                        break
            except httpx.HTTPError:
                break

    return all_events


async def fetch_lck_nearby(target_date: date) -> dict:
    """target_date 기준 가장 최근 완료 경기 / 다음 예정 경기를 반환."""
    all_events = await _fetch_all_events(target_date)

    recent_date: date | None = None
    next_date: date | None = None

    for ev in all_events:
        game = _parse_event(ev)
        if not game:
            continue
        ev_date = date.fromisoformat(game["date"])

        if ev_date < target_date and game["status"] == "경기 종료":
            if recent_date is None or ev_date > recent_date:
                recent_date = ev_date
        elif ev_date > target_date and game["status"] == "경기 전":
            if next_date is None or ev_date < next_date:
                next_date = ev_date

    recent_games = []
    next_games = []

    for ev in all_events:
        game = _parse_event(ev)
        if not game:
            continue
        ev_date = date.fromisoformat(game["date"])
        if recent_date and ev_date == recent_date:
            recent_games.append(game)
        if next_date and ev_date == next_date:
            next_games.append(game)

    return {
        "recent_date": recent_date.isoformat() if recent_date else None,
        "recent_games": recent_games,
        "next_date": next_date.isoformat() if next_date else None,
        "next_games": next_games,
    }


async def fetch_lck_schedule(target_date: date | None = None) -> list[dict]:
    """LCK 경기 일정 반환 (Riot LoL Esports API)."""
    if target_date is None:
        target_date = date.today()

    all_events = await _fetch_all_events(target_date)
    games = _parse_events(all_events, target_date)

    logger.info("Fetched %d LCK games for %s", len(games), target_date.isoformat())
    return games
