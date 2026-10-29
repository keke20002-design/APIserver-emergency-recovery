import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta

import httpx
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# game_id → 생성된 코멘트 (서버 프로세스 내 영구 유지)
_comment_cache: dict[str, str] = {}

NAVER_SPORTS_SCHEDULE_URL = "https://api-gw.sports.naver.com/schedule/games"
NAVER_SPORTS_RELAY_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/relay"
NAVER_SPORTS_RECORD_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/record"
NAVER_SPORTS_PREVIEW_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/preview"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://sports.naver.com/kfootball/schedule/index",
}

# 구장명 → 티켓 링크
TICKET_LINKS: dict[str, str] = {
    "서울": "https://www.ticketlink.co.kr/sports/football/93",
    "수원": "https://www.ticketlink.co.kr/sports/football/94",
    "전주": "https://www.ticketlink.co.kr/sports/football/95",
    "울산": "https://www.ticketlink.co.kr/sports/football/96",
    "대구": "https://www.ticketlink.co.kr/sports/football/97",
    "포항": "https://www.ticketlink.co.kr/sports/football/98",
    "인천": "https://www.ticketlink.co.kr/sports/football/99",
    "광주": "https://www.ticketlink.co.kr/sports/football/100",
    "대전": "https://www.ticketlink.co.kr/sports/football/101",
    "강원": "https://www.ticketlink.co.kr/sports/football/102",
    "제주": "https://www.ticketlink.co.kr/sports/football/103",
    "김천": "https://www.ticketlink.co.kr/sports/football/104",
}

# relay API eventType → 앱 event type 변환
_EVENT_TYPE_MAP: dict[str, str] = {
    "GL": "goal",
    "OGL": "own_goal",
    "YC": "yellow_card",
    "RC": "red_card",
    "PKG": "penalty",   # 페널티골
    "PKM": "penalty",   # 페널티 실축 (보류)
}

# record API timeline eventType → 앱 event type
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


def _parse_score_players_html(html: str) -> list[dict]:
    """
    '<li><span class='time'>33:29</span> 김대원</li>' 형태 HTML 파싱.
    returns [{"player": "김대원", "time": "33", "is_own_goal": False}, ...]
    """
    results = []
    if not html:
        return results
    pattern = r"<li>.*?<span[^>]*>(\d+):\d+</span>\s*(.*?)</li>"
    for m in re.finditer(pattern, html, re.DOTALL):
        minute_str, player_raw = m.group(1), m.group(2).strip()
        # 태그 제거
        player = re.sub(r"<[^>]+>", "", player_raw).strip()
        is_own = "(자책골)" in player
        player = player.replace("(자책골)", "").strip()
        results.append({
            "player": player,
            "minute": int(minute_str),
            "is_own_goal": is_own,
        })
    return results


async def _fetch_relay_events(client: httpx.AsyncClient, game_id: str) -> dict:
    """Live 또는 종료 경기 relay API → 현재 점수 + 이벤트 목록."""
    try:
        url = NAVER_SPORTS_RELAY_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        relay = data.get("result", {}).get("textRelayData", {})

        home_score = relay.get("homeScore")
        away_score = relay.get("awayScore")
        half = relay.get("half")  # 1 or 2 (int or str)
        status_num = relay.get("statusCode")  # 숫자형 상태코드

        # 반 → 한국어
        half_label = None
        if half is not None:
            half_label = f"{half}하프"

        # 이벤트 파싱 (GL, OGL, YC, RC, PKG만)
        events = []
        text_relays = relay.get("textRelays", [])
        for r in text_relays:
            et = r.get("eventType", "")
            app_type = _EVENT_TYPE_MAP.get(et)
            if not app_type:
                continue
            time_str = r.get("time", "")
            # "+3", "90'", "45'" 등 → 숫자만
            minute = re.sub(r"[^\d]", "", time_str)
            half_val = r.get("half", "1")
            # 2하프면 +45
            try:
                min_int = int(minute) if minute else 0
                if str(half_val) == "2" and min_int <= 45:
                    min_int += 45
            except ValueError:
                min_int = 0

            is_home = r.get("homeOrAway", "home") == "home"
            events.append({
                "type": app_type,
                "player": r.get("playerName", ""),
                "minute": min_int,
                "isHome": is_home,
            })

        # 시간 역순 → 분 오름차순 정렬
        events.sort(key=lambda e: e["minute"])

        logger.info("[K리그relay] game=%s | %s-%s | half=%s | events=%d",
                    game_id, home_score, away_score, half_label, len(events))

        return {
            "home_score": home_score,
            "away_score": away_score,
            "half": half_label,
            "events": events,
        }
    except Exception as e:
        logger.warning("Failed to fetch relay for kleague game %s: %s", game_id, e)
        return {"home_score": None, "away_score": None, "half": None, "events": []}


async def _fetch_record_events(client: httpx.AsyncClient, game_id: str) -> list[dict]:
    """경기 종료 후 record API → 골/카드 이벤트."""
    try:
        url = NAVER_SPORTS_RECORD_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        timeline = data.get("result", {}).get("recordData", {}).get("timeline", [])

        events = []
        for item in timeline:
            et = item.get("eventType", "")
            app_type = _RECORD_EVENT_MAP.get(et)
            if not app_type:
                continue
            time_str = item.get("time", "0'")
            minute = int(re.sub(r"[^\d]", "", time_str) or "0")
            side = item.get("side", "home")
            text = item.get("text", "")
            # "김대원 골" → 첫 단어가 이름
            player = text.split()[0] if text else ""
            events.append({
                "type": app_type,
                "player": player,
                "minute": minute,
                "isHome": side == "home",
            })

        events.sort(key=lambda e: e["minute"])
        return events
    except Exception as e:
        logger.warning("Failed to fetch record for kleague game %s: %s", game_id, e)
        return []


async def _fetch_preview(client: httpx.AsyncClient, game_id: str) -> dict:
    """경기 전 preview API → 주요 선수 정보."""
    try:
        url = NAVER_SPORTS_PREVIEW_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
        preview = data.get("result", {}).get("previewData", {})

        def _top5(key: str) -> list[dict]:
            return [
                {
                    "name": p.get("playerName", ""),
                    "goals": p.get("goals", 0),
                    "assists": p.get("assists", 0),
                }
                for p in preview.get(key, [])[:3]
            ]

        home_record = preview.get("hometeam_record", {})
        away_record = preview.get("awayteam_record", {})

        return {
            "home_top_players": _top5("home_team_best5_players"),
            "away_top_players": _top5("away_team_best5_players"),
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
        }
    except Exception as e:
        logger.warning("Failed to fetch preview for kleague game %s: %s", game_id, e)
        return {
            "home_top_players": [],
            "away_top_players": [],
            "home_record": None,
            "away_record": None,
        }


async def _generate_comment(game: dict) -> str | None:
    """경기 전 Gemini 코멘트 생성 (실패 시 None)."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    home = game["home_team"]
    away = game["away_team"]
    hr = game.get("home_record")
    ar = game.get("away_record")

    home_info = f"{hr['rank']}위 {hr['won']}승{hr['drawn']}무{hr['lost']}패" if hr else "순위 미정"
    away_info = f"{ar['rank']}위 {ar['won']}승{ar['drawn']}무{ar['lost']}패" if ar else "순위 미정"

    home_players = game.get("home_top_players", [])
    away_players = game.get("away_top_players", [])
    home_player_str = ", ".join(
        f"{p['name']}({p['goals']}골)" for p in home_players[:2]
    ) if home_players else "미정"
    away_player_str = ", ".join(
        f"{p['name']}({p['goals']}골)" for p in away_players[:2]
    ) if away_players else "미정"

    match_importance = game.get("match_importance") or ""

    prompt = (
        "K리그 경기 관전 포인트를 작성해줘.\n\n"
        "목적: 직관을 고민하는 팬이 경기에 흥미를 느끼도록 만드는 것이다.\n\n"
        "입력 데이터:\n"
        f"- 홈팀: {home} ({home_info})\n"
        f"- 원정팀: {away} ({away_info})\n"
        f"- 주목 선수: {home}의 {home_player_str} / {away}의 {away_player_str}\n"
        + (f"- 경기 중요도: {match_importance}\n" if match_importance else "")
        + "\n조건:\n"
        "- 2문장 이하\n"
        "- 80자 이하\n"
        "- 기사체 금지\n"
        "- 중계 프리뷰 스타일\n"
        "- 경기의 긴장감과 기대감을 강조\n"
        "- '~다' 형태로 끝낼 것\n"
        "- '홈 이점', '변수다' 같은 상투적 표현은 피할 것\n"
        "- 번호·기호 없이 텍스트만 출력"
    )

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.7),
        )
        text = (response.text or "").strip()
        return text if text else None
    except Exception as e:
        logger.warning("_generate_comment failed for %s vs %s: %s", home, away, e)
        return None


async def _batch_generate_comments(games: list[dict], date_str: str) -> None:
    """백그라운드: 경기 코멘트 병렬 생성 후 kleague 캐시 무효화."""
    from utils.cache import cache  # 순환 import 방지를 위해 지역 import
    results = await asyncio.gather(
        *[_generate_comment(g) for g in games],
        return_exceptions=True,
    )
    updated = 0
    for game, res in zip(games, results):
        if isinstance(res, str) and res:
            gid = game.get("game_id", "")
            if gid:
                _comment_cache[gid] = res
                updated += 1
    if updated:
        # 해당 날짜 캐시 + 다가올 경기 캐시 무효화 → 다음 요청에서 코멘트 포함 응답 반환
        cache.delete(f"kleague:{date_str}")
        cache.delete("kleague:today")
        cache.delete_prefix("kleague:upcoming:")
        logger.info("Comments generated for %d games on %s, cache invalidated", updated, date_str)


async def fetch_kleague_schedule(target_date: date | None = None) -> list[dict]:
    """K리그1 경기 일정 + 실시간 스코어 + 이벤트 반환."""
    if target_date is None:
        target_date = date.today()

    date_str = target_date.strftime("%Y-%m-%d")

    params = {
        "fields": "basic,schedule",
        "upperCategoryId": "kfootball",
        "categoryId": "kleague",
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
        logger.error("Failed to fetch K리그 schedule: %s", e)
        raise RuntimeError(f"Failed to fetch K리그 schedule: {e}")

    games = _parse_schedule(data, date_str)
    if not games:
        return games

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        live_games = [g for g in games if g["status"] == "경기 중" and g["game_id"]]
        finished_games = [g for g in games if g["status"] == "경기 종료" and g["game_id"]]
        before_games = [g for g in games if g["status"] == "경기 전" and g["game_id"]]

        # 경기 중: relay (실시간 스코어 + 이벤트)
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

        # 경기 종료: record (확정 이벤트)
        if finished_games:
            record_results = await asyncio.gather(
                *[_fetch_record_events(client, g["game_id"]) for g in finished_games],
                return_exceptions=True,
            )
            for game, res in zip(finished_games, record_results):
                if isinstance(res, list):
                    game["events"] = res

        # 경기 전: preview (선수 정보)
        if before_games:
            preview_results = await asyncio.gather(
                *[_fetch_preview(client, g["game_id"]) for g in before_games],
                return_exceptions=True,
            )
            for game, res in zip(before_games, preview_results):
                if isinstance(res, dict):
                    game["home_top_players"] = res.get("home_top_players", [])
                    game["away_top_players"] = res.get("away_top_players", [])
                    game["home_record"] = res.get("home_record")
                    game["away_record"] = res.get("away_record")

        # 경기 전: 캐시된 코멘트 적용 + 없으면 백그라운드 생성 (응답 블로킹 없음)
        if before_games:
            missing = []
            for game in before_games:
                gid = game.get("game_id", "")
                if gid and gid in _comment_cache:
                    game["comment"] = _comment_cache[gid]
                else:
                    missing.append(game)
            if missing:
                asyncio.create_task(_batch_generate_comments(missing, date_str))

    logger.info("Fetched %d K리그1 games for %s (live=%d, finished=%d, before=%d)",
                len(games), date_str, len(live_games), len(finished_games), len(before_games))
    return games


async def fetch_kleague_upcoming(days: int = 7, limit: int = 12, max_days: int = 45) -> list[dict]:
    """앞으로 N일간 '경기 전' 상태인 K리그1 경기를 모아서 반환.

    날짜 하나씩 3개씩 배치로 조회 (Naver API 동시 요청 과다 시 403 발생 이력 있음).
    충분한 경기를 찾으면 남은 날짜는 조회하지 않고 종료.
    days 이내에 경기가 하나도 없으면 max_days까지 이어서 더 찾아본다.
    """
    today = date.today()
    collected: list[dict] = []

    async def _scan(start_day: int, end_day: int) -> None:
        for start in range(start_day, end_day, 3):
            batch_dates = [today + timedelta(days=start + i) for i in range(min(3, end_day - start))]
            results = await asyncio.gather(
                *[fetch_kleague_schedule(d) for d in batch_dates],
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, list):
                    collected.extend(g for g in res if g.get("status") == "경기 전")
            if len(collected) >= limit:
                return

    await _scan(0, days)
    if not collected and max_days > days:
        await _scan(days, max_days)

    collected.sort(key=lambda g: (g.get("date", ""), g.get("time", "")))
    return collected[:limit]


def _parse_schedule(data: dict, date_str: str) -> list[dict]:
    items = data.get("result", {}).get("games", [])
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

        if cancelled:
            status = "경기 취소"
        else:
            status = _determine_status(status_code)

        display_time = ""
        if game_time_raw:
            try:
                dt = datetime.fromisoformat(str(game_time_raw).replace("Z", "+00:00"))
                display_time = dt.strftime("%H:%M")
            except (ValueError, AttributeError):
                display_time = str(game_time_raw)

        # 스코어 (경기 중/종료만)
        score_str = None
        if status in ("경기 중", "경기 종료", "하프타임") and home_score is not None and away_score is not None:
            score_str = f"{away_score}:{home_score}"

        # 티켓 링크
        ticket_url = ""
        for key, url in TICKET_LINKS.items():
            if key in stadium or key in home_team:
                ticket_url = url
                break

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
            "ticket_url": ticket_url,
            "date": date_str,
            # 경기 전 preview 데이터
            "home_top_players": [],
            "away_top_players": [],
            "home_record": None,
            "away_record": None,
            "comment": None,
        })

    logger.info("Parsed %d K리그1 games for %s", len(games), date_str)
    return games
