import json
import logging
from datetime import date, datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_PAGE_URL = "https://game.naver.com/esports/League_of_Legends/schedule/ewc_lol"

_STATUS_MAP = {
    "BEFORE": "경기 전",
    "READY": "경기 전",
    "RESULT": "경기 종료",
    "STARTED": "경기 중",
    "LIVE": "경기 중",
    "PLAYING": "경기 중",
    "CANCEL": "경기 취소",
    "POSTPONE": "경기 연기",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://game.naver.com/esports/League_of_Legends/schedule/ewc_lol",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 네이버가 nameAcronym을 한글로 내려주는 경우 영문 코드로 정규화
_TEAM_CODE_MAP: dict[str, str] = {
    "한화생명e스포츠": "HLE",
    "한화생명": "HLE",
    "DN수퍼스": "DNS",
    "농심레드포스": "NS",
    "농심": "NS",
    "T1": "T1",
    "젠지": "GEN",
    "Gen.G": "GEN",
    "디플러스 기아": "DK",
    "DPlus KIA": "DK",
    "KT 롤스터": "KT",
    "KT Rolster": "KT",
    "BNK 피어엑스": "BNK",
    "BNK FearX": "BNK",
    "OK브리온": "BRO",
    "OK Brion": "BRO",
    "광동 프릭스": "KDF",
    "Kwangdong Freecs": "KDF",
}


def _normalize_code(name: str, acronym: str) -> str:
    if name in _TEAM_CODE_MAP:
        return _TEAM_CODE_MAP[name]
    if acronym in _TEAM_CODE_MAP:
        return _TEAM_CODE_MAP[acronym]
    # acronym이 ASCII만 포함하면 그대로 사용
    if acronym and all(ord(c) < 128 for c in acronym):
        return acronym
    return ""


def _parse_game(g: dict) -> dict | None:
    home = g.get("homeTeam") or {}
    away = g.get("awayTeam") or {}
    if not home.get("name") or not away.get("name"):
        return None

    start_ms = g.get("startDate")
    if not start_ms:
        return None
    dt_kst = datetime.fromtimestamp(start_ms / 1000, tz=_KST)

    state = g.get("matchStatus", "BEFORE")
    status = _STATUS_MAP.get(state, "경기 전")

    home_score = g.get("homeScore")
    away_score = g.get("awayScore")
    score = None
    if state in ("RESULT", "STARTED", "LIVE", "PLAYING") and home_score is not None:
        score = f"{away_score}:{home_score}"

    return {
        "game_id": g.get("gameId", ""),
        "home_team": home.get("name", ""),
        "away_team": away.get("name", ""),
        "home_code": _normalize_code(home.get("name", ""), home.get("nameAcronym", "")),
        "away_code": _normalize_code(away.get("name", ""), away.get("nameAcronym", "")),
        "home_emblem": home.get("imageUrl", ""),
        "away_emblem": away.get("imageUrl", ""),
        "stadium": g.get("stadium") or "온라인 대회",
        "time": dt_kst.strftime("%H:%M"),
        "status": status,
        "score": score,
        "home_score": home_score if state in ("RESULT", "STARTED", "LIVE") else None,
        "away_score": away_score if state in ("RESULT", "STARTED", "LIVE") else None,
        "date": dt_kst.date().isoformat(),
        "block_name": g.get("title", ""),
        "home_record": None,
        "away_record": None,
    }


async def _fetch_month_games(year_month: str) -> list[dict]:
    """YYYY-MM 기준 game.naver.com Road to EWC 페이지에서 경기 목록 추출."""
    url = f"{_PAGE_URL}?date={year_month}"
    async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
        except httpx.HTTPError as e:
            logger.error("EWC page fetch failed: %s", e)
            raise RuntimeError(f"EWC 일정 조회 실패: {e}")

    idx = html.find('"monthSchedule":[')
    if idx == -1:
        return []

    chunk = html[idx + len('"monthSchedule":'):]
    depth = 0
    end = 0
    for i, ch in enumerate(chunk):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == 0:
        return []

    try:
        month_schedule = json.loads(chunk[:end])
    except json.JSONDecodeError as e:
        logger.warning("EWC monthSchedule parse error: %s", e)
        return []

    games = []
    for group in month_schedule:
        for raw in group.get("schedules", []):
            g = _parse_game(raw)
            if g:
                games.append(g)

    logger.info("Parsed %d EWC games for %s", len(games), year_month)
    return games


async def fetch_ewc_schedule(target_date: date | None = None) -> list[dict]:
    if target_date is None:
        target_date = date.today()

    all_games = await _fetch_month_games(target_date.strftime("%Y-%m"))
    games = [g for g in all_games if date.fromisoformat(g["date"]) == target_date]

    logger.info("Fetched %d EWC games for %s", len(games), target_date.isoformat())
    return games


async def fetch_ewc_standings() -> list[dict]:
    """Road to EWC 팀별 진행 현황. 각 gameId는 시리즈 전체(BO3/BO5) 결과."""
    today = date.today()
    months: set[str] = {today.strftime("%Y-%m")}
    if today.month == 1:
        months.add(f"{today.year - 1}-12")
    else:
        months.add(f"{today.year}-{today.month - 1:02d}")

    all_games: list[dict] = []
    for m in months:
        try:
            all_games.extend(await _fetch_month_games(m))
        except RuntimeError:
            pass

    # team_name → team info (name as key to prevent code/name duplicates)
    teams: dict[str, dict] = {}

    def ensure(name: str, code: str, emblem: str) -> None:
        if name not in teams:
            teams[name] = {
                "code": code,
                "name": name,
                "emblem": emblem,
                "series_wins": 0,
                "series_losses": 0,
                "eliminated": False,
                "latest_round": "",
            }
        elif code and not teams[name]["code"]:
            teams[name]["code"] = code

    for g in all_games:
        hn = g["home_team"]
        an = g["away_team"]
        hc = g["home_code"] or ""
        ac = g["away_code"] or ""
        ensure(hn, hc, g["home_emblem"])
        ensure(an, ac, g["away_emblem"])

        if g["status"] != "경기 종료":
            # 아직 진행 중인 라운드 기록
            if not teams[hn]["latest_round"]:
                teams[hn]["latest_round"] = g["block_name"]
            if not teams[an]["latest_round"]:
                teams[an]["latest_round"] = g["block_name"]
            continue

        hs = g["home_score"] or 0
        as_ = g["away_score"] or 0
        block = g["block_name"]

        if hs > as_:
            teams[hn]["series_wins"] += 1
            teams[hn]["latest_round"] = block
            teams[an]["series_losses"] += 1
            teams[an]["eliminated"] = True
            if not teams[an]["latest_round"]:
                teams[an]["latest_round"] = block
        elif as_ > hs:
            teams[an]["series_wins"] += 1
            teams[an]["latest_round"] = block
            teams[hn]["series_losses"] += 1
            teams[hn]["eliminated"] = True
            if not teams[hn]["latest_round"]:
                teams[hn]["latest_round"] = block

    result = list(teams.values())
    # 탈락팀은 아래로, 승수 많은 팀 위로
    result.sort(key=lambda x: (x["eliminated"], -x["series_wins"]))
    for i, t in enumerate(result):
        t["rank"] = i + 1

    return result


async def fetch_ewc_nearby(target_date: date) -> dict:
    months = {target_date.strftime("%Y-%m")}
    if target_date.month == 1:
        months.add(f"{target_date.year - 1}-12")
    else:
        months.add(f"{target_date.year}-{target_date.month - 1:02d}")
    if target_date.month == 12:
        months.add(f"{target_date.year + 1}-01")
    else:
        months.add(f"{target_date.year}-{target_date.month + 1:02d}")

    all_games: list[dict] = []
    for m in months:
        try:
            all_games.extend(await _fetch_month_games(m))
        except RuntimeError:
            pass

    recent_date: date | None = None
    next_date: date | None = None

    for g in all_games:
        ev_date = date.fromisoformat(g["date"])
        if ev_date < target_date and g["status"] == "경기 종료":
            if recent_date is None or ev_date > recent_date:
                recent_date = ev_date
        elif ev_date > target_date and g["status"] == "경기 전":
            if next_date is None or ev_date < next_date:
                next_date = ev_date

    return {
        "recent_date": recent_date.isoformat() if recent_date else None,
        "recent_games": [g for g in all_games if recent_date and date.fromisoformat(g["date"]) == recent_date],
        "next_date": next_date.isoformat() if next_date else None,
        "next_games": [g for g in all_games if next_date and date.fromisoformat(g["date"]) == next_date],
    }
