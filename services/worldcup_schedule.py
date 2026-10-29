import asyncio
import logging
import re
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

NAVER_SPORTS_SCHEDULE_URL = "https://api-gw.sports.naver.com/schedule/games"
NAVER_SPORTS_STANDINGS_URL = "https://api-gw.sports.naver.com/record/standings"
NAVER_SPORTS_RELAY_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/relay"
NAVER_SPORTS_RECORD_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/record"

WORLDCUP_CATEGORY_ID = "worldcup"
WORLDCUP_UPPER_CATEGORY = "wfootball"
WORLDCUP_SEASON_CODE = "3F9X"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://sports.naver.com/fifaworldcup2026/schedule/index",
}

# 토너먼트 라운드 → 날짜 범위
WORLDCUP_ROUNDS = [
    {"round": "조별리그", "from": "2026-06-11", "to": "2026-06-27"},
    {"round": "32강",    "from": "2026-06-28", "to": "2026-07-02"},
    {"round": "16강",    "from": "2026-07-04", "to": "2026-07-07"},
    {"round": "8강",     "from": "2026-07-09", "to": "2026-07-10"},
    {"round": "4강",     "from": "2026-07-14", "to": "2026-07-15"},
    {"round": "결승",    "from": "2026-07-19", "to": "2026-07-19"},
]

# 2026 FIFA 월드컵 개최 경기장 16개 (정적 데이터)
WORLDCUP_STADIUMS = [
    {"name": "메트라이프 스타디움",        "name_en": "MetLife Stadium",               "city": "East Rutherford, NJ", "country": "미국",   "capacity": 82500},
    {"name": "AT&T 스타디움",              "name_en": "AT&T Stadium",                  "city": "Arlington, TX",       "country": "미국",   "capacity": 80000},
    {"name": "소파이 스타디움",            "name_en": "SoFi Stadium",                  "city": "Inglewood, CA",       "country": "미국",   "capacity": 70240},
    {"name": "NRG 스타디움",              "name_en": "NRG Stadium",                   "city": "Houston, TX",         "country": "미국",   "capacity": 72220},
    {"name": "엠파워 필드",               "name_en": "Empower Field at Mile High",    "city": "Denver, CO",          "country": "미국",   "capacity": 76125},
    {"name": "애로우헤드 스타디움",        "name_en": "Arrowhead Stadium",             "city": "Kansas City, MO",     "country": "미국",   "capacity": 76416},
    {"name": "리바이스 스타디움",          "name_en": "Levi's Stadium",                "city": "Santa Clara, CA",     "country": "미국",   "capacity": 68500},
    {"name": "앨리전트 스타디움",          "name_en": "Allegiant Stadium",             "city": "Las Vegas, NV",       "country": "미국",   "capacity": 65000},
    {"name": "하드록 스타디움",            "name_en": "Hard Rock Stadium",             "city": "Miami Gardens, FL",   "country": "미국",   "capacity": 64767},
    {"name": "링컨 파이낸셜 필드",         "name_en": "Lincoln Financial Field",       "city": "Philadelphia, PA",    "country": "미국",   "capacity": 69796},
    {"name": "질레트 스타디움",            "name_en": "Gillette Stadium",              "city": "Foxborough, MA",      "country": "미국",   "capacity": 65878},
    {"name": "BC 플레이스",               "name_en": "BC Place",                      "city": "Vancouver, BC",       "country": "캐나다", "capacity": 54500},
    {"name": "BMO 필드",                  "name_en": "BMO Field",                     "city": "Toronto, ON",         "country": "캐나다", "capacity": 45736},
    {"name": "에스타디오 아즈테카",        "name_en": "Estadio Azteca",               "city": "Mexico City",         "country": "멕시코", "capacity": 87523},
    {"name": "에스타디오 BBVA",           "name_en": "Estadio BBVA",                  "city": "Monterrey",           "country": "멕시코", "capacity": 53500},
    {"name": "에스타디오 아크론",          "name_en": "Estadio Akron",                "city": "Guadalajara",         "country": "멕시코", "capacity": 49850},
]

_EVENT_TYPE_MAP: dict[str, str] = {
    "GL": "goal",
    "OGL": "own_goal",
    "YC": "yellow_card",
    "RC": "red_card",
    "PKG": "penalty",
    "PKM": "penalty",
}

_RECORD_EVENT_MAP: dict[str, str] = {
    "goal": "goal",
    "ownGoal": "own_goal",
    "yellow": "yellow_card",
    "red": "red_card",
    "penalty": "penalty",
}


def _determine_status(status_code: str) -> str:
    status_map = {
        "BEFORE": "경기 전",
        "READY": "경기 전",
        "STARTED": "경기 중",
        "LIVE": "경기 중",
        "PLAYING": "경기 중",
        "FIRSTHALF": "경기 중",
        "HALFTIME": "하프타임",
        "SECONDHALF": "경기 중",
        "RESULT": "경기 종료",
        "AFTER": "경기 종료",
        "FINAL": "경기 종료",
        "END": "경기 종료",
        "ENDED": "경기 종료",
        "FINISH": "경기 종료",
        "CANCEL": "경기 취소",
        "POSTPONE": "경기 연기",
        "DELAY": "경기 연기",
    }
    return status_map.get(status_code, status_code or "미정")


def _get_current_round(target_date: date) -> str:
    date_str = target_date.strftime("%Y-%m-%d")
    for r in WORLDCUP_ROUNDS:
        if r["from"] <= date_str <= r["to"]:
            return r["round"]
    return ""


async def _fetch_relay_events(client: httpx.AsyncClient, game_id: str) -> dict:
    try:
        url = NAVER_SPORTS_RELAY_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        relay = data.get("result", {}).get("textRelayData", {})

        home_score = relay.get("homeScore")
        away_score = relay.get("awayScore")
        half = relay.get("half")
        half_label = f"{half}하프" if half is not None else None

        events = []
        for r in relay.get("textRelays", []):
            et = r.get("eventType", "")
            app_type = _EVENT_TYPE_MAP.get(et)
            if not app_type:
                continue
            time_str = r.get("time", "")
            minute = re.sub(r"[^\d]", "", time_str)
            half_val = r.get("half", "1")
            try:
                min_int = int(minute) if minute else 0
                if str(half_val) == "2" and min_int <= 45:
                    min_int += 45
            except ValueError:
                min_int = 0
            events.append({
                "type": app_type,
                "player": r.get("playerName", ""),
                "minute": min_int,
                "isHome": r.get("homeOrAway", "home") == "home",
            })

        events.sort(key=lambda e: e["minute"])
        return {"home_score": home_score, "away_score": away_score, "half": half_label, "events": events}
    except Exception as e:
        logger.warning("Failed to fetch relay for worldcup game %s: %s", game_id, e)
        return {"home_score": None, "away_score": None, "half": None, "events": []}


async def _fetch_game_detail(client: httpx.AsyncClient, game_id: str) -> dict:
    """개별 게임 상세 API → scorers(골 정보) + groupName.

    Naver record/relay API는 월드컵 미지원(recordData/textRelayData null).
    /schedule/games/{game_id}의 scorers 필드를 대신 사용.
    반환: {"events": [...], "group": "A"}
    """
    try:
        url = f"https://api-gw.sports.naver.com/schedule/games/{game_id}"
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        game = data.get("result", {}).get("game", {})

        group = game.get("groupName", "")  # "A"~"L"

        scorers = game.get("scorers", {}) or {}
        home_scorers = scorers.get("home", []) or []
        away_scorers = scorers.get("away", []) or []

        events: list[dict] = []
        for scorer in home_scorers:
            minute = int(scorer.get("time", 0))
            added = int(scorer.get("addedTime", 0))
            if added > 0:
                minute += added  # 45+3 → 48
            events.append({
                "type": "own_goal" if scorer.get("ownGoal") else "goal",
                "player": scorer.get("playerName", ""),
                "minute": minute,
                "isHome": True,
            })
        for scorer in away_scorers:
            minute = int(scorer.get("time", 0))
            added = int(scorer.get("addedTime", 0))
            if added > 0:
                minute += added
            events.append({
                "type": "own_goal" if scorer.get("ownGoal") else "goal",
                "player": scorer.get("playerName", ""),
                "minute": minute,
                "isHome": False,
            })

        events.sort(key=lambda e: e["minute"])
        logger.info("Fetched game detail for %s: group=%s events=%d", game_id, group, len(events))
        return {"events": events, "group": group}
    except Exception as e:
        logger.warning("Failed to fetch game detail for worldcup game %s: %s", game_id, e)
        return {"events": [], "group": ""}


def _parse_schedule(data: dict, date_str: str) -> list[dict]:
    items = data.get("result", {}).get("games", [])
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    round_name = _get_current_round(target_date)
    games = []

    for g in items:
        game_id = g.get("gameId", "")
        home_team = g.get("homeTeamName", "")
        away_team = g.get("awayTeamName", "")
        home_emblem = g.get("homeTeamEmblemUrl", "")
        away_emblem = g.get("awayTeamEmblemUrl", "")
        stadium = g.get("stadium", "")
        game_time_raw = g.get("gameDateTime", "")
        status_code = g.get("statusCode", "")
        home_score = g.get("homeTeamScore")
        away_score = g.get("awayTeamScore")
        cancelled = g.get("cancel", False)
        # Naver가 조 정보를 groupName 또는 leagueName 등으로 줄 수 있음
        group = g.get("groupName", g.get("leagueName", g.get("categoryName", "")))

        status = "경기 취소" if cancelled else _determine_status(status_code)

        display_time = ""
        if game_time_raw:
            try:
                dt = datetime.fromisoformat(str(game_time_raw).replace("Z", "+00:00"))
                display_time = dt.strftime("%H:%M")
            except (ValueError, AttributeError):
                display_time = str(game_time_raw)

        score_str = None
        if status in ("경기 중", "경기 종료", "하프타임") and home_score is not None and away_score is not None:
            score_str = f"{away_score}:{home_score}"

        games.append({
            "game_id": game_id,
            "home_team": home_team,
            "away_team": away_team,
            "home_emblem": home_emblem,
            "away_emblem": away_emblem,
            "stadium": stadium,
            "time": display_time,
            "status": status,
            "score": score_str,
            "home_score": home_score if status in ("경기 중", "경기 종료", "하프타임") else None,
            "away_score": away_score if status in ("경기 중", "경기 종료", "하프타임") else None,
            "half": None,
            "events": [],
            "group": group,
            "round": round_name,
            "date": date_str,
        })

    logger.info("Parsed %d worldcup games for %s", len(games), date_str)
    return games


async def fetch_worldcup_schedule(target_date: date | None = None) -> list[dict]:
    """FIFA 월드컵 2026 경기 일정 + 실시간 스코어 + 이벤트."""
    if target_date is None:
        target_date = date.today()

    date_str = target_date.strftime("%Y-%m-%d")
    params = {
        "fields": "basic,schedule",
        "upperCategoryId": WORLDCUP_UPPER_CATEGORY,
        "categoryId": WORLDCUP_CATEGORY_ID,
        "fromDate": date_str,
        "toDate": date_str,
        "size": "100",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            resp = await client.get(NAVER_SPORTS_SCHEDULE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Failed to fetch worldcup schedule: %s", e)
        raise RuntimeError(f"Failed to fetch worldcup schedule: {e}")

    games = _parse_schedule(data, date_str)
    if not games:
        return games

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        live_games = [g for g in games if g["status"] == "경기 중" and g["game_id"]]
        finished_games = [g for g in games if g["status"] == "경기 종료" and g["game_id"]]

        if live_games:
            relay_results = await asyncio.gather(
                *[_fetch_relay_events(client, g["game_id"]) for g in live_games],
                return_exceptions=True,
            )
            for game, res in zip(live_games, relay_results):
                if isinstance(res, dict):
                    game["home_score"] = res.get("home_score", game["home_score"])
                    game["away_score"] = res.get("away_score", game["away_score"])
                    game["half"] = res.get("half")
                    game["events"] = res.get("events", [])

        if finished_games:
            detail_results = await asyncio.gather(
                *[_fetch_game_detail(client, g["game_id"]) for g in finished_games],
                return_exceptions=True,
            )
            for game, res in zip(finished_games, detail_results):
                if isinstance(res, dict):
                    game["events"] = res.get("events", [])
                    if res.get("group"):
                        game["group"] = res["group"]

    logger.info("Fetched %d worldcup games for %s (live=%d, finished=%d)",
                len(games), date_str, len(live_games), len(finished_games))
    return games


async def fetch_worldcup_standings() -> dict:
    """FIFA 월드컵 2026 조별 순위.

    Naver Sports record/standings API가 월드컵 그룹 순위를 지원하지 않아
    조별리그 경기 결과에서 직접 계산한다.
    """
    group_stage_from = "2026-06-11"
    group_stage_to   = "2026-06-27"
    params = {
        "fields": "basic,schedule",
        "upperCategoryId": WORLDCUP_UPPER_CATEGORY,
        "categoryId": WORLDCUP_CATEGORY_ID,
        "fromDate": group_stage_from,
        "toDate": group_stage_to,
        "size": "200",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            resp = await client.get(NAVER_SPORTS_SCHEDULE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Failed to fetch worldcup group stage games: %s", e)
        return {}

    games_raw = data.get("result", {}).get("games", [])
    return _compute_standings_from_games(games_raw)


def _compute_standings_from_games(games_raw: list) -> dict:
    """조별리그 경기 결과에서 조별 순위를 계산한다.

    Naver API는 게임 객체에 조(group) 정보를 주지 않으므로,
    서로 대전한 팀 조합을 클러스터링해 4팀씩 그룹을 구성한다.
    """
    # team → {"played", "won", "drawn", "lost", "gf", "ga", "points", "emblem", "opponents": set}
    teams: dict[str, dict] = {}

    def ensure(name: str, emblem: str) -> None:
        if name not in teams:
            teams[name] = {
                "team": name,
                "emblem": emblem,
                "played": 0,
                "won": 0,
                "drawn": 0,
                "lost": 0,
                "goals_for": 0,
                "goals_against": 0,
                "points": 0,
                "_opponents": set(),
            }

    # 경기 결과 집계
    played_pairs: list[tuple] = []
    for g in games_raw:
        home = g.get("homeTeamName", "")
        away = g.get("awayTeamName", "")
        hs = g.get("homeTeamScore")
        as_ = g.get("awayTeamScore")
        status = g.get("statusCode", "")
        h_emblem = g.get("homeTeamEmblemUrl", "")
        a_emblem = g.get("awayTeamEmblemUrl", "")

        if not home or not away:
            continue

        ensure(home, h_emblem)
        ensure(away, a_emblem)
        teams[home]["_opponents"].add(away)
        teams[away]["_opponents"].add(home)
        played_pairs.append((home, away))

        if status not in ("RESULT", "AFTER", "FINAL", "END", "ENDED", "FINISH"):
            continue
        if hs is None or as_ is None:
            continue

        hs, as_ = int(hs), int(as_)
        teams[home]["played"] += 1
        teams[away]["played"] += 1
        teams[home]["goals_for"] += hs
        teams[home]["goals_against"] += as_
        teams[away]["goals_for"] += as_
        teams[away]["goals_against"] += hs

        if hs > as_:
            teams[home]["won"] += 1
            teams[home]["points"] += 3
            teams[away]["lost"] += 1
        elif hs < as_:
            teams[away]["won"] += 1
            teams[away]["points"] += 3
            teams[home]["lost"] += 1
        else:
            teams[home]["drawn"] += 1
            teams[home]["points"] += 1
            teams[away]["drawn"] += 1
            teams[away]["points"] += 1

    if not teams:
        return {}

    # 상대전적 집합 기반 Union-Find 클러스터링으로 조(group) 구성
    parent: dict[str, str] = {t: t for t in teams}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for home, away in played_pairs:
        union(home, away)

    clusters: dict[str, list[str]] = {}
    for team in teams:
        root = find(team)
        clusters.setdefault(root, []).append(team)

    # 조 이름 부여 (A, B, C...)
    group_labels = "ABCDEFGHIJKL"
    groups: dict[str, list[dict]] = {}
    for idx, (_, members) in enumerate(sorted(clusters.items(), key=lambda x: -len(x[1]))):
        label = group_labels[idx] if idx < len(group_labels) else str(idx + 1)
        table = []
        for t in members:
            info = {k: v for k, v in teams[t].items() if not k.startswith("_")}
            info["goal_difference"] = info["goals_for"] - info["goals_against"]
            info["rank"] = 0
            table.append(info)
        table.sort(key=lambda x: (-x["points"], -x["goal_difference"], -x["goals_for"]))
        for i, row in enumerate(table):
            row["rank"] = i + 1
        groups[label] = table

    logger.info("Computed worldcup standings: %d groups from %d games", len(groups), len(games_raw))
    return groups


def _parse_standings(data: dict) -> dict:
    """순위 응답을 조별(A~L)로 파싱. Naver 응답 구조에 따라 다양한 형태 처리."""
    result = data.get("result", {})
    groups: dict[str, list[dict]] = {}

    # 형태 1: standings가 그룹 배열인 경우
    # [{"groupName": "A", "teamList": [...]}, ...]
    raw_standings = result.get("standings", result.get("rankList", []))

    if isinstance(raw_standings, list) and raw_standings:
        first = raw_standings[0]
        if isinstance(first, dict) and ("teamList" in first or "groupName" in first):
            for group_data in raw_standings:
                group_name = group_data.get("groupName", group_data.get("name", ""))
                team_list = group_data.get("teamList", group_data.get("teams", []))
                if group_name and team_list:
                    groups[group_name] = [_parse_team_row(t) for t in team_list]
        else:
            # 형태 2: 각 팀 row에 groupName 필드가 있는 경우
            for row in raw_standings:
                group_name = row.get("groupName", row.get("group", ""))
                if group_name:
                    groups.setdefault(group_name, []).append(_parse_team_row(row))
                else:
                    groups.setdefault("전체", []).append(_parse_team_row(row))

    # 각 조 내 순위 정렬
    for group_name in groups:
        groups[group_name].sort(key=lambda x: x["rank"])

    logger.info("Parsed worldcup standings: %d groups", len(groups))
    return groups


def _parse_team_row(row: dict) -> dict:
    team_name = row.get("teamName", row.get("name", ""))
    emblem = row.get("teamEmblemUrl", row.get("emblemUrl", ""))
    rank = row.get("rank", row.get("position", 0))
    played = row.get("played", row.get("games", 0))
    won = row.get("won", row.get("win", 0))
    drawn = row.get("drawn", row.get("draw", 0))
    lost = row.get("lost", row.get("lose", 0))
    gf = row.get("goalsFor", row.get("goalFor", 0))
    ga = row.get("goalsAgainst", row.get("goalAgainst", 0))
    gd = row.get("goalDifference", row.get("goalsDiff", gf - ga))
    points = row.get("points", row.get("point", won * 3 + drawn))

    return {
        "rank": rank,
        "team": team_name,
        "emblem": emblem,
        "played": played,
        "won": won,
        "drawn": drawn,
        "lost": lost,
        "goals_for": gf,
        "goals_against": ga,
        "goal_difference": gd,
        "points": points,
    }


async def fetch_worldcup_bracket() -> list[dict]:
    """토너먼트 브래킷 (32강~결승). 각 라운드의 날짜 범위로 schedule API 호출."""
    knockout_rounds = [r for r in WORLDCUP_ROUNDS if r["round"] != "조별리그"]
    bracket = []

    for round_info in knockout_rounds:
        try:
            params = {
                "fields": "basic,schedule",
                "upperCategoryId": WORLDCUP_UPPER_CATEGORY,
                "categoryId": WORLDCUP_CATEGORY_ID,
                "fromDate": round_info["from"],
                "toDate": round_info["to"],
                "size": "100",
            }
            async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
                resp = await client.get(NAVER_SPORTS_SCHEDULE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()

            games = _parse_schedule(data, round_info["from"])
            # round 필드를 현재 라운드명으로 강제 설정
            for g in games:
                g["round"] = round_info["round"]

            bracket.append({
                "round": round_info["round"],
                "date_from": round_info["from"],
                "date_to": round_info["to"],
                "games": games,
            })
        except Exception as e:
            logger.warning("Failed to fetch bracket round %s: %s", round_info["round"], e)
            bracket.append({
                "round": round_info["round"],
                "date_from": round_info["from"],
                "date_to": round_info["to"],
                "games": [],
            })

    logger.info("Fetched worldcup bracket: %d rounds", len(bracket))
    return bracket
