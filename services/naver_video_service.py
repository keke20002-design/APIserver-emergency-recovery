import json
import logging
import re
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_SCHEDULE_URL = "https://game.naver.com/esports/League_of_Legends/schedule/lck"
_VIDEO_URL = "https://game.naver.com/esports/League_of_Legends/videos/{video_id}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://game.naver.com/esports/League_of_Legends/",
}


def _extract_next_data(html: str) -> dict | None:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


async def _fetch_schedule_games(year: int, month: int) -> list[dict]:
    """Fetches Naver LCK schedule for given year/month and returns game list."""
    url = _SCHEDULE_URL
    params = {}
    now = datetime.now(_KST)
    if year != now.year or month != now.month:
        params = {"date": f"{year}{month:02d}"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        resp = await client.get(url, headers=_HEADERS, params=params)
        resp.raise_for_status()
        html = resp.text

    data = _extract_next_data(html)
    if not data:
        logger.warning("No __NEXT_DATA__ in Naver schedule page")
        return []

    month_sched = (
        data.get("props", {})
        .get("initialState", {})
        .get("schedule", {})
        .get("monthSchedule", [])
    )

    games = []
    for group in month_sched:
        for game in group.get("schedules", []):
            games.append(game)
    return games


def _teams_match(game: dict, home_code: str, away_code: str) -> bool:
    """Check if game teams match the given codes (order-independent)."""
    home = game.get("homeTeam", {}).get("nameEngAcronym", "").upper()
    away = game.get("awayTeam", {}).get("nameEngAcronym", "").upper()
    home_kr = game.get("homeTeam", {}).get("nameAcronym", "").upper()
    away_kr = game.get("awayTeam", {}).get("nameAcronym", "").upper()
    hc, ac = home_code.upper(), away_code.upper()
    return (
        (home == hc and away == ac)
        or (home == ac and away == hc)
        or (home_kr == hc and away_kr == ac)
        or (home_kr == ac and away_kr == hc)
    )


async def _find_replay_video_id(
    home_code: str, away_code: str, date_str: str
) -> int | None:
    """Find Naver replayVideoId for a match by team codes and date.

    Naver only serves the current split's schedule regardless of the requested
    month, so we fetch the current split once and:
    1. First try exact month-day match (for current-split games).
    2. Fall back to the closest completed game between those teams in the
       current split (for cross-split lookups from team-detail recent matches).
    """
    try:
        dt = datetime.fromisoformat(date_str)
    except ValueError:
        logger.warning("Invalid date: %s", date_str)
        return None

    now_kst = datetime.now(_KST)
    # Always fetch the current split (Naver ignores historical date params)
    games = await _fetch_schedule_games(now_kst.year, now_kst.month)
    if not games:
        return None

    # Pass 1: exact month-day match (handles current-split games)
    for game in games:
        if not _teams_match(game, home_code, away_code):
            continue
        start_ms = game.get("startDate", 0)
        game_date = datetime.fromtimestamp(start_ms / 1000, tz=_KST).date()
        if game_date.month == dt.month and game_date.day == dt.day:
            vid = game.get("replayVideoId")
            if vid:
                return int(vid)

    # Pass 2: date is outside the current split – return the most-recent
    # completed game between these teams in the current split
    candidates = [
        g for g in games
        if _teams_match(g, home_code, away_code) and g.get("replayVideoId")
    ]
    if candidates:
        # pick the latest one
        best = max(candidates, key=lambda g: g.get("startDate", 0))
        logger.info(
            "Cross-split fallback: returning latest replay for %s vs %s (requested %s)",
            home_code, away_code, date_str,
        )
        return int(best["replayVideoId"])

    logger.info(
        "No Naver replay found for %s vs %s on %s", home_code, away_code, date_str
    )
    return None


async def _fetch_game_videos(replay_video_id: int) -> list[dict]:
    """Fetch all VOD clips for a match from the Naver video detail page."""
    url = _VIDEO_URL.format(video_id=replay_video_id)
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text

    data = _extract_next_data(html)
    if not data:
        return []

    game_videos = (
        data.get("props", {})
        .get("initialState", {})
        .get("video", {})
        .get("gameVideo", [])
    )

    result = []
    for v in game_videos:
        vid_id = v.get("id")
        if not vid_id:
            continue
        result.append(
            {
                "naver_video_id": vid_id,
                "title": v.get("title", ""),
                "image_url": v.get("imageUrl", ""),
                "naver_url": _VIDEO_URL.format(video_id=vid_id),
                "play_time": v.get("playTime", 0),
            }
        )
    return result


async def fetch_naver_lck_replay(
    home_code: str, away_code: str, date_str: str
) -> list[dict]:
    """
    Main entry: returns list of Naver VOD cards for a finished LCK match.
    Each item: {naver_video_id, title, image_url, naver_url, play_time}
    """
    replay_id = await _find_replay_video_id(home_code, away_code, date_str)
    if replay_id is None:
        return []

    videos = await _fetch_game_videos(replay_id)
    return videos
