import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

NAVER_SPORTS_SCHEDULE_URL = "https://api-gw.sports.naver.com/schedule/games"
NAVER_SPORTS_RELAY_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/relay"
NAVER_SPORTS_RECORD_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/record"
NAVER_SPORTS_PREVIEW_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/preview"
NAVER_SPORTS_LINEUP_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/lineup"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://sports.naver.com/wfootball/schedule/index",
}

from services.epl_team_names import en_team as _en_team
from services.epl_stadium_names import en_stadium as _en_stadium
from services.epl_player_names import fetch_lineup_name_map as _fetch_player_name_map
from services.epl_player_names import fetch_bench_names as _fetch_bench_names
from services.epl_player_names import resolve_substitute_name as _resolve_substitute_name

_EVENT_TYPE_MAP: dict[str, str] = {
    "GL": "goal",
    "OGL": "own_goal",
    "YC": "yellow_card",
    "RC": "red_card",
    "PKG": "penalty",
    "PKM": "penalty_miss",
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


async def _resolve_player_name(
    client: httpx.AsyncClient, game_id: str, home_team: str, away_team: str,
    match_date: date, name_map: dict[str, str], raw_name: str, is_home: bool,
) -> str:
    """Exact starting-XI match first (name_map, position-slot matched —
    see epl_player_names.py); if that misses, fuzzy-match against the
    bench candidates for this side. Falls back to the original Korean
    name if neither finds a confident match — never guesses blindly."""
    if not raw_name:
        return raw_name
    if raw_name in name_map:
        return name_map[raw_name]
    bench = await _fetch_bench_names(client, game_id, home_team, away_team, match_date)
    candidates = bench["home"] if is_home else bench["away"]
    return _resolve_substitute_name(raw_name, candidates) or raw_name


async def _fetch_relay_events(
    client: httpx.AsyncClient, game_id: str,
    home_team: str = "", away_team: str = "", match_date: date | None = None,
) -> dict:
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

        name_map = {}
        if match_date is not None:
            name_map = await _fetch_player_name_map(client, game_id, home_team, away_team, match_date)

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
            is_home = r.get("homeOrAway", "home") == "home"
            player_name = r.get("playerName", "")
            resolved_name = player_name
            if player_name and match_date is not None:
                resolved_name = await _resolve_player_name(
                    client, game_id, home_team, away_team, match_date, name_map, player_name, is_home
                )
            events.append({
                "type": app_type,
                "player": resolved_name,
                "minute": min_int,
                "isHome": is_home,
            })

        events.sort(key=lambda e: e["minute"])
        return {"home_score": home_score, "away_score": away_score, "half": half_label, "events": events}
    except Exception as e:
        logger.warning("EPL relay fetch failed for %s: %s", game_id, e)
        return {"home_score": None, "away_score": None, "half": None, "events": []}


async def _fetch_record_events(
    client: httpx.AsyncClient, game_id: str,
    home_team: str = "", away_team: str = "", match_date: date | None = None,
) -> list[dict]:
    try:
        url = NAVER_SPORTS_RECORD_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        # .get(key, {}) only falls back when the key is *missing* — Naver
        # sends recordData: null (key present) for some finished matches
        # with no detailed record yet, which .get() passes through as None
        # and crashes the next .get("timeline") call below.
        record_data = data.get("result", {}).get("recordData") or {}
        timeline = record_data.get("timeline", [])
        logger.info("EPL record %s: recordData keys=%s timeline_len=%d",
                    game_id, list(record_data.keys()), len(timeline))
        if timeline:
            logger.info("EPL record %s first event: %s", game_id, timeline[0])

        name_map = {}
        if match_date is not None:
            name_map = await _fetch_player_name_map(client, game_id, home_team, away_team, match_date)

        events = []
        for item in timeline:
            et = item.get("eventType", "")
            app_type = _RECORD_EVENT_MAP.get(et)
            if not app_type:
                continue
            time_str = item.get("time", "0'")
            minute = int(re.sub(r"[^\d]", "", time_str) or "0")
            side = item.get("side", "home")
            is_home = side == "home"
            # try direct playerName first, fall back to first word of text
            player = (item.get("playerName") or
                      item.get("player", {}).get("playerName", "") or
                      (item.get("text", "").split()[0] if item.get("text") else ""))
            resolved_name = player
            if player and match_date is not None:
                resolved_name = await _resolve_player_name(
                    client, game_id, home_team, away_team, match_date, name_map, player, is_home
                )
            events.append({
                "type": app_type,
                "player": resolved_name,
                "minute": minute,
                "isHome": is_home,
            })
        events.sort(key=lambda e: e["minute"])
        logger.info("EPL record %s: parsed %d events", game_id, len(events))
        return events
    except Exception as e:
        logger.warning("EPL record fetch failed for %s: %s", game_id, e)
        return []


def _parse_naver_player(p: dict, name_map: dict[str, str] | None = None) -> dict:
    ko_name = p.get("name", "")
    return {
        "name": (name_map or {}).get(ko_name, ko_name),
        "position": p.get("pos", ""),
        "number": str(p.get("shirtNumber") or ""),
        # "0" = starter, "1" = substitute (Naver sends strings)
        "is_starter": str(p.get("substitute", "0")) == "0",
        "goals": int(p.get("goal") or 0),
        "own_goals": int(p.get("ownGoal") or 0),
        "yellow_cards": int(p.get("card") or 0),
        "red_cards": int(p.get("yellowRedCard") or 0),
    }


def _flatten_2d_players(rows, name_map: dict[str, str] | None = None) -> list[dict]:
    """Flatten 2D position-row array into a flat player list."""
    players = []
    if not rows:
        return players
    for row in rows:
        items = row if isinstance(row, list) else [row]
        for p in items:
            if isinstance(p, dict) and p.get("name"):
                players.append(_parse_naver_player(p, name_map))
    return players


def _format_formation(raw: str | None) -> str | None:
    """Naver sends formation as a raw digit string, e.g. '4231' -> '4-2-3-1'."""
    if not raw or not str(raw).isdigit():
        return None
    return "-".join(str(raw))


async def _fetch_lineup(
    client: httpx.AsyncClient, game_id: str,
    home_team: str = "", away_team: str = "", match_date: date | None = None,
) -> dict:
    """Fetch starting lineups — structure: lineUpData.lineup.home/away.players.lineup (2D array)."""
    empty = {
        "home_lineup": [], "away_lineup": [],
        "home_formation": None, "away_formation": None,
    }
    try:
        url = NAVER_SPORTS_LINEUP_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})

        lineup_root = result.get("lineUpData", {}).get("lineup", {})
        if not lineup_root:
            logger.info("EPL lineup %s: no lineUpData.lineup", game_id)
            return empty

        home_node = lineup_root.get("home", {})
        away_node = lineup_root.get("away", {})
        home_rows = home_node.get("players", {}).get("lineup", [])
        away_rows = away_node.get("players", {}).get("lineup", [])

        name_map = {}
        if match_date is not None:
            name_map = await _fetch_player_name_map(client, game_id, home_team, away_team, match_date)

        home = _flatten_2d_players(home_rows, name_map)
        away = _flatten_2d_players(away_rows, name_map)
        logger.info("EPL lineup %s: home=%d away=%d", game_id, len(home), len(away))
        return {
            "home_lineup": home,
            "away_lineup": away,
            "home_formation": _format_formation(home_node.get("formation")),
            "away_formation": _format_formation(away_node.get("formation")),
        }
    except Exception as e:
        logger.warning("EPL lineup fetch failed for %s: %s", game_id, e)
        return empty


async def _fetch_preview(client: httpx.AsyncClient, game_id: str) -> dict:
    empty = {
        "home_top_players": [], "away_top_players": [],
        "home_record": None, "away_record": None, "h2h": [],
    }
    try:
        url = NAVER_SPORTS_PREVIEW_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        preview = data.get("result", {}).get("previewData", {})

        def _top3(key: str) -> list[dict]:
            raw = preview.get(key) or []
            return [
                {
                    "name": p.get("playerName", ""),
                    "goals": p.get("goals", 0),
                    "assists": p.get("assists", 0),
                }
                for p in raw[:3]
            ]

        home_record = preview.get("hometeam_record") or {}
        away_record = preview.get("awayteam_record") or {}

        h2h = [
            {
                "date": g.get("gameDate", ""),
                "home": g.get("homeTeamName", ""),
                "away": g.get("awayTeamName", ""),
                "home_score": g.get("homeTeamScore"),
                "away_score": g.get("awayTeamScore"),
            }
            for g in (preview.get("team_vs_lately_game_resultlist") or [])[:5]
        ]

        return {
            "home_top_players": _top3("home_team_best5_players"),
            "away_top_players": _top3("away_team_best5_players"),
            "home_record": {
                "rank": home_record.get("rank"),
                "won": home_record.get("won"),
                "drawn": home_record.get("drawn"),
                "lost": home_record.get("lost"),
            } if home_record else None,
            "away_record": {
                "rank": away_record.get("rank"),
                "won": away_record.get("won"),
                "drawn": away_record.get("drawn"),
                "lost": away_record.get("lost"),
            } if away_record else None,
            "h2h": h2h,
        }
    except Exception as e:
        logger.warning("EPL preview fetch failed for %s: %s", game_id, e)
        return empty


def _parse_schedule(data: dict, date_str: str) -> list[dict]:
    items = data.get("result", {}).get("games", [])
    games = []
    for g in items:
        game_id = g.get("gameId", "")
        home_team = _en_team(g.get("homeTeamName", ""))
        away_team = _en_team(g.get("awayTeamName", ""))
        home_emblem = g.get("homeTeamEmblemUrl", "")
        away_emblem = g.get("awayTeamEmblemUrl", "")
        stadium = _en_stadium(g.get("stadium", ""))
        game_time_raw = g.get("gameDateTime", "")
        status_code = g.get("statusCode", "")
        home_score = g.get("homeTeamScore")
        away_score = g.get("awayTeamScore")
        cancelled = g.get("cancel", False)

        status = "경기 취소" if cancelled else _determine_status(status_code)

        display_time = ""
        kickoff_utc = None
        if game_time_raw:
            try:
                dt = datetime.fromisoformat(str(game_time_raw).replace("Z", "+00:00"))
                # kickoff_utc carries the real instant so the client can convert
                # to whatever timezone the *device* is actually in — display_time
                # below is KST-only and kept just for any legacy display use;
                # don't use it to reconstruct a DateTime (see kickoff_utc note
                # in match_model.dart's _parseKickoff).
                kickoff_utc = dt.astimezone(timezone.utc).isoformat()
                kst = dt.astimezone(timezone(timedelta(hours=9)))
                display_time = kst.strftime("%H:%M")
            except (ValueError, AttributeError):
                display_time = str(game_time_raw)

        score_str = None
        if status in ("경기 중", "경기 종료", "하프타임") and home_score is not None and away_score is not None:
            score_str = f"{away_score}:{home_score}"

        # Parse scorers into timeline events
        scorers = g.get("scorers") or {}
        events = []
        for s in (scorers.get("home") or []):
            minute = int(s.get("time") or 0) + int(s.get("addedTime") or 0)
            events.append({
                "type": "own_goal" if s.get("ownGoal") else "goal",
                "player": s.get("playerName", ""),
                "minute": minute,
                "isHome": True,
            })
        for s in (scorers.get("away") or []):
            minute = int(s.get("time") or 0) + int(s.get("addedTime") or 0)
            events.append({
                "type": "own_goal" if s.get("ownGoal") else "goal",
                "player": s.get("playerName", ""),
                "minute": minute,
                "isHome": False,
            })
        events.sort(key=lambda e: e["minute"])

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
            "events": events,
            "home_top_players": [],
            "away_top_players": [],
            "home_record": None,
            "away_record": None,
            "home_lineup": [],
            "away_lineup": [],
            "home_formation": None,
            "away_formation": None,
            "h2h": [],
            "date": date_str,
            "kickoff_utc": kickoff_utc,
        })

    logger.info("Parsed %d EPL games for %s", len(games), date_str)
    return games


def _events_from_lineup(home_players: list, away_players: list) -> list[dict]:
    """Build scorer/card events from player stats when relay/record data is unavailable."""
    events = []
    for p in home_players:
        for _ in range(int(p.get("goals", 0))):
            events.append({"type": "goal", "player": p["name"], "minute": 0, "isHome": True})
        for _ in range(int(p.get("own_goals", 0))):
            events.append({"type": "own_goal", "player": p["name"], "minute": 0, "isHome": True})
        for _ in range(int(p.get("yellow_cards", 0))):
            events.append({"type": "yellow_card", "player": p["name"], "minute": 0, "isHome": True})
        for _ in range(int(p.get("red_cards", 0))):
            events.append({"type": "red_card", "player": p["name"], "minute": 0, "isHome": True})
    for p in away_players:
        for _ in range(int(p.get("goals", 0))):
            events.append({"type": "goal", "player": p["name"], "minute": 0, "isHome": False})
        for _ in range(int(p.get("own_goals", 0))):
            events.append({"type": "own_goal", "player": p["name"], "minute": 0, "isHome": False})
        for _ in range(int(p.get("yellow_cards", 0))):
            events.append({"type": "yellow_card", "player": p["name"], "minute": 0, "isHome": False})
        for _ in range(int(p.get("red_cards", 0))):
            events.append({"type": "red_card", "player": p["name"], "minute": 0, "isHome": False})
    return events


async def _fetch_basic_schedule(
    target_date: date, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """Just the day's game list (teams/emblems/date/time/stadium/status) —
    no preview/lineup/relay/record enrichment. Cheap: one Naver call per
    day regardless of how many games that day has. Use this instead of
    fetch_epl_schedule when callers don't need per-match detail (e.g. a
    forward fixture search across many days).

    Pass a shared [client] when calling this many times concurrently (e.g.
    asyncio.gather over 30 days) — each call opening its own
    httpx.AsyncClient means 30 fresh TCP+TLS handshakes to the same host
    at once, which measured ~12s end-to-end vs ~1s when they share one
    connection-pooled client."""
    date_str = target_date.strftime("%Y-%m-%d")
    params = {
        "fields": "basic,schedule,scorers",
        "upperCategoryId": "wfootball",
        "categoryId": "epl",
        "fromDate": date_str,
        "toDate": date_str,
        "size": "100",
    }

    try:
        if client is not None:
            resp = await client.get(NAVER_SPORTS_SCHEDULE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        else:
            async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as c:
                resp = await c.get(NAVER_SPORTS_SCHEDULE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
    except httpx.HTTPError as e:
        logger.error("EPL schedule fetch failed: %s", e)
        raise RuntimeError(f"Failed to fetch EPL schedule: {e}")

    return _parse_schedule(data, date_str)


async def fetch_epl_schedule(
    target_date: date | None = None, client: httpx.AsyncClient | None = None,
    translate_names: bool = True,
) -> list[dict]:
    """EPL 경기 일정 + 실시간 스코어 + 이벤트 반환.

    Pass a shared [client] when calling this many times concurrently (see
    _fetch_basic_schedule's docstring / CLAUDE.md's httpx.AsyncClient
    sharing note) — otherwise each call opens up to 2 of its own clients,
    which serializes badly under asyncio.gather over many days.

    [translate_names]=False skips every player-name translation call (each
    of which hits API-Football) for every game this returns — set this when
    scanning many days' worth of games but only a handful (or one) will
    actually get used, e.g. fetch_team_last_formation's 21-day backward
    search: translating all ~10 games/day x 21 days just to discard all but
    one match was the actual cause of the API-Football rate-limit storm
    (dozens of concurrent lookups, each firing 429s) — callers doing a
    targeted single-game lookup should translate that one result themselves
    afterward instead."""
    if target_date is None:
        target_date = date.today()

    date_str = target_date.strftime("%Y-%m-%d")
    games = await _fetch_basic_schedule(target_date, client=client)
    if not games:
        return games

    if client is not None:
        return await _enrich_schedule(games, date_str, client, target_date, translate_names)
    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as c:
        return await _enrich_schedule(games, date_str, c, target_date, translate_names)


async def _enrich_schedule(
    games: list[dict], date_str: str, client: httpx.AsyncClient, target_date: date,
    translate_names: bool = True,
) -> list[dict]:
    # Threaded into every _fetch_relay_events/_fetch_record_events/_fetch_lineup
    # call below as their `match_date` arg — those functions already treat
    # match_date=None as "skip translation" (used whenever a caller doesn't
    # pass team names either), so reusing that same switch here avoids a
    # second translate/don't-translate code path.
    name_translate_date = target_date if translate_names else None
    live_games = [g for g in games if g["status"] == "경기 중" and g["game_id"]]
    finished_games = [g for g in games if g["status"] == "경기 종료" and g["game_id"]]
    before_games = [g for g in games if g["status"] == "경기 전" and g["game_id"]]

    if live_games:
        relay_results = await asyncio.gather(
            *[_fetch_relay_events(client, g["game_id"], g["home_team"], g["away_team"], name_translate_date)
              for g in live_games],
            return_exceptions=True,
        )
        for game, res in zip(live_games, relay_results):
            if isinstance(res, dict):
                game["home_score"] = res.get("home_score", game["home_score"])
                game["away_score"] = res.get("away_score", game["away_score"])
                game["half"] = res.get("half")
                # relay events override scorers for live games (real-time accuracy)
                relay_events = res.get("events", [])
                if relay_events:
                    game["events"] = relay_events

    if finished_games:
        record_results = await asyncio.gather(
            *[_fetch_record_events(client, g["game_id"], g["home_team"], g["away_team"], name_translate_date)
              for g in finished_games],
            return_exceptions=True,
        )
        for game, res in zip(finished_games, record_results):
            # only overwrite scorers-derived events if record has richer data
            if isinstance(res, list) and res:
                game["events"] = res

    # preview (season stats + top players + H2H): fetch for finished AND before games
    preview_target = finished_games + before_games
    if preview_target:
        preview_results = await asyncio.gather(
            *[_fetch_preview(client, g["game_id"]) for g in preview_target],
            return_exceptions=True,
        )
        for game, res in zip(preview_target, preview_results):
            if isinstance(res, dict):
                game["home_top_players"] = res.get("home_top_players", [])
                game["away_top_players"] = res.get("away_top_players", [])
                game["home_record"] = res.get("home_record")
                game["away_record"] = res.get("away_record")
                game["h2h"] = res.get("h2h", [])

    # lineup: fetch for all games
    lineup_target = finished_games + live_games + before_games
    if lineup_target:
        lineup_results = await asyncio.gather(
            *[_fetch_lineup(client, g["game_id"], g["home_team"], g["away_team"], name_translate_date)
              for g in lineup_target],
            return_exceptions=True,
        )
        for game, res in zip(lineup_target, lineup_results):
            if isinstance(res, dict):
                game["home_lineup"] = res.get("home_lineup", [])
                game["away_lineup"] = res.get("away_lineup", [])
                game["home_formation"] = res.get("home_formation")
                game["away_formation"] = res.get("away_formation")

    # Final events pass: translate any player names still in Korean —
    # covers the _parse_schedule scorers fallback (used when relay/record
    # had nothing for this match, e.g. Naver sends recordData: null for
    # some finished games) which builds "events" before this function ever
    # runs and has no name translation of its own. Reuses the name_map the
    # lineup fetch above already cached per game_id, so this is a cache hit
    # (no extra network calls) for every game that just went through it.
    if translate_names:
        for game in live_games + finished_games:
            if not game.get("events"):
                continue
            name_map = await _fetch_player_name_map(
                client, game["game_id"], game["home_team"], game["away_team"], target_date
            )
            for e in game["events"]:
                e["player"] = await _resolve_player_name(
                    client, game["game_id"], game["home_team"], game["away_team"],
                    target_date, name_map, e["player"], e.get("isHome", True),
                )

    logger.info("Fetched %d EPL games for %s (live=%d, finished=%d, before=%d)",
                len(games), date_str, len(live_games), len(finished_games), len(before_games))
    return games


async def fetch_team_last_formation(team_name: str) -> dict:
    """The team's most recently *finished* match's formation AND starting
    lineup — real data from a past match, never a guess at what they'll
    play next. Used client-side as a fallback (labeled "last match" in the
    UI, never presented as confirmed) when a scheduled match's own lineup
    hasn't been announced yet — once Naver publishes the real lineup for
    the upcoming match, the caller has that data too and should prefer it
    over this. Searches up to 120 days back (covers international breaks
    and the summer off-season) with one range query, then fetches only the
    team's latest match days in detail.

    fetch_epl_schedule (not the cheaper basic-schedule fetch) already pulls
    each finished game's lineup via _fetch_lineup for its own "before_games"
    preview needs, so the player list here is free — no extra requests.

    translate_names=False for the scan: translating every
    game's lineup/events across ~10 games/day x 21 days just to discard all
    but one match was flooding API-Football with dozens of concurrent
    lookups and triggering sustained 429s. Only the ONE match that actually
    matches [team_name] gets translated (below), after the scan finds it."""
    empty = {"formation": None, "opponent": None, "date": None, "lineup": []}
    today = date.today()
    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        # 예전엔 21일을 하루씩 스캔해서 여름 비시즌(~3개월) 뒤엔 항상 빈 값이었다.
        # 120일 범위 조회 1번으로 이 팀이 뛴 날짜만 골라낸 뒤, 가장 최근 3개
        # 날짜만 상세 조회한다(최근 경기에 포메이션이 없을 때를 대비한 여유분).
        try:
            items = await _fetch_range_basic(today - timedelta(days=120), today - timedelta(days=1), client)
        except httpx.HTTPError as e:
            logger.error("EPL range fetch failed: %s", e)
            return empty
        team_dates = sorted(
            {
                date.fromisoformat(g["gameDate"])
                for g in items
                if g.get("gameDate")
                and team_name in (_en_team(g.get("homeTeamName", "")), _en_team(g.get("awayTeamName", "")))
            },
            reverse=True,
        )[:3]
        results = await asyncio.gather(
            *[fetch_epl_schedule(d, client=client, translate_names=False) for d in team_dates],
            return_exceptions=True,
        )

        # Closest day first (list order), same "most recent match" result as
        # the old sequential loop.
        for games in results:
            if not isinstance(games, list):
                continue
            for g in games:
                if g["status"] != "경기 종료":
                    continue
                if g["home_team"] == team_name and g.get("home_formation"):
                    formation, opponent, lineup_side = g["home_formation"], g["away_team"], g.get("home_lineup", [])
                elif g["away_team"] == team_name and g.get("away_formation"):
                    formation, opponent, lineup_side = g["away_formation"], g["home_team"], g.get("away_lineup", [])
                else:
                    continue

                name_map = await _fetch_player_name_map(
                    client, g["game_id"], g["home_team"], g["away_team"], date.fromisoformat(g["date"])
                )
                lineup = [
                    {**p, "name": name_map.get(p["name"], p["name"])} for p in lineup_side
                ]
                return {
                    "formation": formation,
                    "opponent": opponent,
                    "date": g["date"],
                    "lineup": lineup,
                }
    return empty


async def fetch_team_upcoming_fixtures(team_name: str, limit: int = 5) -> list[dict]:
    """[team_name]'s next [limit] scheduled fixtures. One Naver range query
    over the next 120 days (covers international breaks and the summer
    off-season) instead of the old 30 per-day requests, then grouped by
    date and parsed with the same _parse_schedule the daily endpoint uses —
    no per-match preview/lineup calls since this only needs
    date/time/teams."""
    today = date.today()
    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        try:
            items = await _fetch_range_basic(
                today, today + timedelta(days=120), client, fields="basic,schedule",
            )
        except httpx.HTTPError as e:
            logger.error("EPL range fetch failed: %s", e)
            return []

    by_date: dict[str, list[dict]] = {}
    for g in items:
        if g.get("gameDate"):
            by_date.setdefault(g["gameDate"], []).append(g)
    results = [
        _parse_schedule({"result": {"games": by_date[d]}}, d) for d in sorted(by_date)
    ]

    fixtures: list[dict] = []
    for games in results:
        if not isinstance(games, list):
            continue
        for g in games:
            # Exclude known non-upcoming statuses rather than whitelisting
            # one "scheduled" string — mirrors Flutter's own status parsing
            # (match_model.dart's _parseStatus), which defaults anything
            # unrecognized to scheduled instead of the other way round.
            if g["status"] in ("경기 종료", "경기 중", "하프타임", "경기 연기", "경기 취소"):
                continue
            if g["home_team"] != team_name and g["away_team"] != team_name:
                continue
            fixtures.append({
                "game_id": g["game_id"],
                "home_team": g["home_team"],
                "away_team": g["away_team"],
                "home_emblem": g.get("home_emblem"),
                "away_emblem": g.get("away_emblem"),
                "date": g["date"],
                "time": g.get("time"),
                "kickoff_utc": g.get("kickoff_utc"),
                "stadium": g.get("stadium"),
                "is_home": g["home_team"] == team_name,
            })
            if len(fixtures) >= limit:
                return fixtures
    return fixtures


async def _fetch_range_basic(
    from_date: date, to_date: date, client: httpx.AsyncClient,
    fields: str = "basic",
) -> list[dict]:
    """[from_date]~[to_date] 구간의 EPL 경기 원본 목록(fields=basic).
    범위 조회 1번으로 끝나므로 기간이 길어도 비용이 같다."""
    params = {
        "fields": fields,
        "upperCategoryId": "wfootball",
        "categoryId": "epl",
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
        "size": "500",
    }
    resp = await client.get(NAVER_SPORTS_SCHEDULE_URL, params=params)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("games", [])


async def _fetch_game_dates(
    from_date: date, to_date: date, client: httpx.AsyncClient,
) -> list[date]:
    """[from_date]~[to_date] 구간에서 EPL 경기가 있는 날짜 목록(정렬됨)."""
    items = await _fetch_range_basic(from_date, to_date, client)
    return sorted({date.fromisoformat(g["gameDate"]) for g in items if g.get("gameDate")})


async def fetch_epl_nearby(target_date: date) -> dict:
    """경기 없는 날 — 최근 완료 경기 + 다음 예정 경기 반환.

    예전엔 앞뒤 ±7일을 하루씩 조회했는데, A매치 휴식기(마지막 경기~다음
    경기 사이가 2~3주)나 여름 비시즌(~3개월)에는 7일 안에 경기가 없어
    recent/upcoming이 둘 다 비고 앱에 "경기 없음"만 떴다. 이제 과거/미래
    각 120일을 범위 조회 1번씩으로 훑어 "가장 가까운 경기일"을 찾고,
    그 두 날짜만 fetch_epl_schedule로 상세 조회한다.
    """
    window = timedelta(days=120)

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        past_dates, future_dates = await asyncio.gather(
            _fetch_game_dates(target_date - window, target_date - timedelta(days=1), client),
            _fetch_game_dates(target_date + timedelta(days=1), target_date + window, client),
            return_exceptions=True,
        )
        # 가장 가까운 날짜 순으로 후보 2개씩 — 가장 가까운 날이 전부 연기/취소라
        # 필터 후 비는 경우를 대비해 한 날짜 더 본다.
        past_days = list(reversed(past_dates))[:2] if isinstance(past_dates, list) else []
        upcoming_days = future_dates[:2] if isinstance(future_dates, list) else []

        past_results, upcoming_results = await asyncio.gather(
            asyncio.gather(
                *[fetch_epl_schedule(d, client=client) for d in past_days],
                return_exceptions=True,
            ),
            asyncio.gather(
                *[fetch_epl_schedule(d, client=client) for d in upcoming_days],
                return_exceptions=True,
            ),
        )

    # 가장 가까운 날짜부터(리스트 순서 그대로) 첫 매치가 있는 날을 채택 —
    # 순차 루프였을 때와 동일한 "가장 가까운 날" 선택 결과를 유지한다.
    past_games: list[dict] = []
    for games in past_results:
        if not isinstance(games, list):
            continue
        finished = [g for g in games if g["status"] == "경기 종료"]
        if finished:
            past_games = finished
            break

    upcoming_games: list[dict] = []
    for games in upcoming_results:
        if not isinstance(games, list):
            continue
        before = [g for g in games if g["status"] == "경기 전"]
        if before:
            upcoming_games = before
            break

    return {
        "date": target_date.isoformat(),
        "recent_games": past_games,
        "upcoming_games": upcoming_games,
    }
