"""API-Football (RapidAPI, host apifootball3.p.rapidapi.com) pre-match 1X2
odds — used ONLY for odds. Fixtures, teams, stats and lineups stay on Naver
Sports as before (plan.md 2차 범위: Odds 용도로만 추가).

This is the legacy apifootball.com-style single-endpoint API (everything
goes through `?action=...`, confirmed against the user's actual RapidAPI
subscription — NOT the newer api-football-v1 REST API this module was
originally written against, which the user was never actually subscribed
to). Naver and this provider don't share match ids, so a fixture is
resolved by EPL league + kickoff date + team-name match.

No LLM, nothing invented: any missing piece (no key configured, fixture
not found, no bookmaker odds posted yet) returns None and the caller
hides the market UI rather than guessing.
"""
import asyncio
import logging
import os
from datetime import date as date_type, timedelta

import httpx

from utils.cache import cache

logger = logging.getLogger(__name__)

_HOST = "apifootball3.p.rapidapi.com"
_BASE_URL = f"https://{_HOST}"
_EPL_LEAGUE_ID = "152"  # this provider's own internal id for the Premier League
_BOOKMAKER = "bet365"  # user's explicit choice — one consistent bookmaker, never mixed

# RapidAPI's free/basic tier 429s easily under burst concurrency — e.g.
# fetch_team_last_formation's 21-day backward scan can fan out dozens of
# lineup/odds lookups (each hitting this host at least once, some twice) all
# at the same instant via asyncio.gather. A process-wide semaphore caps how
# many of *those* requests are ever in flight together, and epl_player_names.py
# reuses this same semaphore (via _get below) rather than having its own, so
# the cap is real across the whole app, not just within one call site.
_RATE_LIMIT = asyncio.Semaphore(3)


def _headers() -> dict | None:
    key = os.getenv("API_FOOTBALL_KEY")
    if not key:
        return None
    return {"x-rapidapi-key": key, "x-rapidapi-host": _HOST}


def _norm(name: str) -> str:
    return name.lower().replace(" ", "")


async def _get(client: httpx.AsyncClient, params: dict) -> httpx.Response:
    """Rate-limited GET to API-Football, with one retry on 429 — every
    caller (odds, match lookup, lineups) goes through this instead of
    client.get() directly so the semaphore and backoff apply everywhere."""
    async with _RATE_LIMIT:
        resp = await client.get(_BASE_URL, params=params)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 1.5
            logger.warning("API-Football 429, retrying once after %.1fs (params=%s)", delay, params)
            await asyncio.sleep(delay)
            resp = await client.get(_BASE_URL, params=params)
        return resp


# In-flight get_events requests, keyed by date — callers awaiting the same
# date share one Task instead of each firing their own request. A plain TTL
# cache alone doesn't cover this: fetch_team_last_formation's 21-day scan
# (services/epl_schedule.py) fans out many games sharing the same day via
# asyncio.gather, and they all start *before* any of them could have
# populated the cache — this dict is what collapses that burst down to one
# real request per date.
_events_inflight: dict[str, asyncio.Task] = {}


async def _fetch_events_for_date(client: httpx.AsyncClient, match_date: date_type) -> list:
    """Raw get_events response for the ±1-day window around [match_date],
    cached AND in-flight-coalesced by date so every game on the same day
    (there can be several) shares one request instead of each re-querying
    the same window."""
    cache_key = f"epl_af_events_v1:{match_date.isoformat()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    existing = _events_inflight.get(cache_key)
    if existing is not None:
        return await existing

    async def _do_fetch() -> list:
        try:
            resp = await _get(
                client,
                params={
                    "action": "get_events",
                    "from": (match_date - timedelta(days=1)).isoformat(),
                    "to": (match_date + timedelta(days=1)).isoformat(),
                    "league_id": _EPL_LEAGUE_ID,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        finally:
            _events_inflight.pop(cache_key, None)
        # e.g. {"error": 404, "message": "No event found..."} when nothing's scheduled that day
        events = data if isinstance(data, list) else []
        cache.set(cache_key, events, ttl=1800)
        return events

    task = asyncio.ensure_future(_do_fetch())
    _events_inflight[cache_key] = task
    return await task


async def _find_match_id(
    client: httpx.AsyncClient, home_team: str, away_team: str, match_date: date_type
) -> str | None:
    # +-1 day window: Naver buckets kickoffs by KST calendar day while this
    # provider buckets by the match's local (UK) day, so a late-evening UK
    # kickoff commonly lands on "the next day" in Naver's date but not here
    # (and vice versa near midnight). A single exact-day query silently
    # misses those — this is the actual cause seen in testing (Naver said
    # 2026-09-19, the provider had it filed under 2026-09-18).
    data = await _fetch_events_for_date(client, match_date)

    home_n, away_n = _norm(home_team), _norm(away_team)
    for match in data:
        mh = _norm(match.get("match_hometeam_name", ""))
        ma = _norm(match.get("match_awayteam_name", ""))
        if (home_n in mh or mh in home_n) and (away_n in ma or ma in away_n):
            return match.get("match_id")
    return None


async def fetch_match_odds(
    home_team: str, away_team: str, match_date: date_type
) -> dict | None:
    """Pre-match 1X2 odds for one EPL fixture, or None if unavailable."""
    headers = _headers()
    if headers is None:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            match_id = await _find_match_id(client, home_team, away_team, match_date)
            if match_id is None:
                return None

            resp = await _get(
                client, params={"action": "get_odds", "match_id": match_id}
            )
            resp.raise_for_status()
            rows = resp.json()
    except httpx.HTTPError as e:
        logger.warning("API-Football odds fetch failed for %s vs %s: %s", home_team, away_team, e)
        return None

    if not isinstance(rows, list):
        return None

    # bet365 specifically, per the user's request — never silently
    # substitute a different bookmaker's odds under the bet365 label.
    for row in rows:
        if row.get("odd_bookmakers") != _BOOKMAKER:
            continue
        home_odds, draw_odds, away_odds = row.get("odd_1"), row.get("odd_x"), row.get("odd_2")
        if not (home_odds and draw_odds and away_odds):
            return None
        try:
            return {
                "bookmaker": _BOOKMAKER,
                "home_odds": float(home_odds),
                "draw_odds": float(draw_odds),
                "away_odds": float(away_odds),
            }
        except ValueError:
            return None
    return None


def compute_market_probability(home_odds: float, draw_odds: float, away_odds: float) -> dict:
    """Odds -> normalized implied probability (removes bookmaker overround
    so the three values sum to ~100, not >100)."""
    raw_home = 1 / home_odds
    raw_draw = 1 / draw_odds
    raw_away = 1 / away_odds
    total = raw_home + raw_draw + raw_away

    home_pct = round(raw_home / total * 100)
    draw_pct = round(raw_draw / total * 100)
    away_pct = 100 - home_pct - draw_pct
    return {"home": home_pct, "draw": draw_pct, "away": away_pct}
