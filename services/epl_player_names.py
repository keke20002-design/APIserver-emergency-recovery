"""Korean -> English EPL player name translation for one specific match.

Naver Sports (this app's main data source) only ever returns a Korean
`name` field for players — there's no English field and no ID we could
cross-reference against an English-name source, and the player set (unlike
the ~35 fixed teams/stadiums) is far too large and volatile for a hand-
maintained dict like epl_team_names.py/epl_stadium_names.py.

Instead this borrows API-Football (apifootball3.p.rapidapi.com, already
used for odds in api_football_odds.py), whose `get_lineups` action does
return English names.

Two matching strategies, depending on what's available:

1. **Starting XI — exact position-slot match.** The two providers don't
   share player IDs, but both number the starting XI 1-11 by the same
   convention (GK=1) — Naver's `positionOrder` and API-Football's
   `lineup_position` line up 1:1, verified against a real match (position
   1 = GK in both, position 10 = the same striker in both). This is an
   exact key match, not a guess.

2. **Substitutes who show up in a Timeline event — fuzzy name match.**
   Naver's `/lineup` endpoint never returns substitutes at all (confirmed
   by scanning 25 days of fixtures — every response had exactly 11 players
   per side, none marked `substitute:"1"`), so a substitute's Korean name
   only ever appears inline in a goal/card event once they're actually
   involved. API-Football gives every substitute `lineup_position: "0"`
   (no distinguishing order), so there's no exact key to match on — instead
   this romanizes the Korean name (Revised Romanization, hand-rolled from
   Unicode Hangul decomposition — no external dependency) and compares it
   against that side's ~9 bench candidates with difflib. This is an
   approximate match and can misfire (e.g. two similarly-named bench
   players), unlike strategy 1 — only used when the caller has no exact
   match, and only returns a name when the best candidate clears both a
   similarity floor and a margin over the runner-up; otherwise the
   original Korean name is kept rather than guessed.
"""
import difflib
import logging
import unicodedata
from datetime import date as date_type

import httpx

from services.api_football_odds import _find_match_id, _get, _headers
from utils.cache import cache

logger = logging.getLogger(__name__)

_NAVER_LINEUP_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/lineup"
_CACHE_TTL = 1800  # 30min — mirrors the schedule cache cadence noted in main.py's scheduler

# Tuned against a real bench (9 candidates, one EPL match) comparing
# romanize(Korean name) to each candidate: 0.6/0.15 missed several true
# matches whose best score landed in the 0.4-0.55 range (Revised
# Romanization diverges a lot from how certain sounds/accented names
# actually get spelled, e.g. "Schär" -> "syeeo" vs "schar") while still
# being the clear best candidate — 0.4/0.12 recovered those (8/9 correct,
# 0 wrong picks; the 1 miss — "Schär" — correctly returned no match rather
# than guessing wrong, since its margin over the runner-up was only ~0.04).
_FUZZY_MIN_SCORE = 0.4
_FUZZY_MIN_MARGIN = 0.12


async def fetch_lineup_name_map(
    client: httpx.AsyncClient, game_id: str, home_team: str, away_team: str, match_date: date_type
) -> dict[str, str]:
    """Korean player name -> English player name, for every starting-XI
    player in this match (both teams combined into one flat dict — Naver
    names are unique enough per-match that callers don't need a home/away
    split). Empty dict if either side has no data yet (API-Football match
    not found, lineup not posted, Naver lineup not announced) — callers
    fall back to the original Korean name, never guess."""
    cache_key = f"epl_player_names_v1:{game_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    naver_by_position = await _fetch_naver_position_names(client, game_id)
    if not naver_by_position:
        cache.set(cache_key, {}, ttl=_CACHE_TTL)
        return {}

    lineup_raw = await _fetch_api_football_lineup_raw(game_id, home_team, away_team, match_date)
    en_by_position = _extract_starter_positions(lineup_raw)

    name_map = {
        ko_name: en_by_position[pos]
        for pos, ko_name in naver_by_position.items()
        if pos in en_by_position
    }
    # Cache the empty-dict result too (e.g. lineup not announced yet) so we
    # don't re-hit both providers on every request for the same not-ready
    # game — 30min TTL means it'll pick up the real lineup soon after posting.
    cache.set(cache_key, name_map, ttl=_CACHE_TTL)
    return name_map


async def fetch_bench_names(
    client: httpx.AsyncClient, game_id: str, home_team: str, away_team: str, match_date: date_type
) -> dict[str, list[str]]:
    """{"home": [English names...], "away": [...]} — the substitutes bench
    for this match, straight from API-Football's get_lineups (no Naver
    involvement — Naver never lists substitutes). Used as the candidate
    pool for fuzzy-matching a Korean substitute name that shows up in a
    Timeline event. Shares the same cached raw API-Football response as
    fetch_lineup_name_map() when both are called for the same game_id, so
    calling this in addition to fetch_lineup_name_map() doesn't add an
    extra HTTP request in the common case."""
    cache_key = f"epl_player_bench_v1:{game_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    lineup_raw = await _fetch_api_football_lineup_raw(game_id, home_team, away_team, match_date)
    bench = _extract_bench_names(lineup_raw)
    cache.set(cache_key, bench, ttl=_CACHE_TTL)
    return bench


def resolve_substitute_name(korean_name: str, candidates: list[str]) -> str | None:
    """Fuzzy-match a Korean substitute name against this side's bench
    candidates (English names from fetch_bench_names()). Returns the best
    match only if it's both above a similarity floor and clearly ahead of
    the runner-up — otherwise None (caller keeps the original Korean name;
    this never picks a "best guess among bad options")."""
    if not korean_name or not candidates:
        return None

    romanized = _romanize_hangul(korean_name).lower()
    if not romanized:
        return None

    scored = []
    for candidate in candidates:
        normalized_full = _strip_accents(candidate).lower()
        surname = normalized_full.split()[-1] if normalized_full.split() else normalized_full
        score = max(
            difflib.SequenceMatcher(None, romanized, surname).ratio(),
            difflib.SequenceMatcher(None, romanized, normalized_full).ratio(),
        )
        scored.append((score, candidate))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_name = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_score >= _FUZZY_MIN_SCORE and (best_score - runner_up_score) >= _FUZZY_MIN_MARGIN:
        return best_name
    return None


# ---------------------------------------------------------------------------
# Naver side


async def _fetch_naver_position_names(client: httpx.AsyncClient, game_id: str) -> dict[str, str]:
    """"home:1".."home:11"/"away:1".."away:11" -> Korean name, starting XI
    only. Home and away each number their own XI 1-11 independently (both
    start from GK=1), so the side prefix is required — without it, e.g.
    home position "1" and away position "1" (two different players) would
    collide in a single merged dict."""
    try:
        url = _NAVER_LINEUP_URL.format(game_id=game_id)
        resp = await client.get(url, timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("EPL player-name lookup: Naver lineup fetch failed for %s: %s", game_id, e)
        return {}

    lineup_root = data.get("result", {}).get("lineUpData", {}).get("lineup", {})
    by_position: dict[str, str] = {}
    for side in ("home", "away"):
        rows = lineup_root.get(side, {}).get("players", {}).get("lineup", [])
        for row in rows or []:
            items = row if isinstance(row, list) else [row]
            for p in items:
                if not isinstance(p, dict):
                    continue
                pos = p.get("positionOrder")
                name = p.get("name")
                # substitute="0" is starter; subs get no reliable position slot to match on
                if pos and name and str(p.get("substitute", "0")) == "0":
                    by_position[f"{side}:{pos}"] = name
    return by_position


# ---------------------------------------------------------------------------
# API-Football side


async def _fetch_api_football_lineup_raw(
    game_id: str, home_team: str, away_team: str, match_date: date_type
) -> dict:
    """The raw {"home": {...}, "away": {...}} lineup node from
    API-Football's get_lineups (both starting_lineups and substitutes),
    cached per game_id so fetch_lineup_name_map() and fetch_bench_names()
    share one HTTP round-trip instead of two.

    Opens its own RapidAPI-headered client rather than reusing a Naver
    client — _find_match_id() (borrowed from api_football_odds.py) makes
    its request with the client's *default* headers, and a Naver client
    carries User-Agent/Referer, not the x-rapidapi-key auth header this
    host needs. Same pattern api_football_odds.fetch_match_odds() uses."""
    cache_key = f"epl_af_lineup_raw_v1:{game_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    headers = _headers()
    if headers is None:
        return {}

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as af_client:
            match_id = await _find_match_id(af_client, home_team, away_team, match_date)
            if match_id is None:
                cache.set(cache_key, {}, ttl=_CACHE_TTL)
                return {}
            resp = await _get(af_client, params={"action": "get_lineups", "match_id": match_id})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("EPL player-name lookup: API-Football lineup fetch failed for %s vs %s: %s",
                        home_team, away_team, e)
        return {}

    match_node = data.get(str(match_id)) if isinstance(data, dict) else None
    lineup = match_node.get("lineup", {}) if match_node else {}
    cache.set(cache_key, lineup, ttl=_CACHE_TTL)
    return lineup


def _extract_starter_positions(lineup_raw: dict) -> dict[str, str]:
    """"home:1".."home:11"/"away:1".."away:11" -> English name."""
    by_position: dict[str, str] = {}
    for side in ("home", "away"):
        for p in lineup_raw.get(side, {}).get("starting_lineups", []) or []:
            pos = p.get("lineup_position")
            name = p.get("lineup_player")
            if pos and pos != "0" and name:
                by_position[f"{side}:{pos}"] = name
    return by_position


def _extract_bench_names(lineup_raw: dict) -> dict[str, list[str]]:
    bench: dict[str, list[str]] = {"home": [], "away": []}
    for side in ("home", "away"):
        for p in lineup_raw.get(side, {}).get("substitutes", []) or []:
            name = p.get("lineup_player")
            if name:
                bench[side].append(name)
    return bench


# ---------------------------------------------------------------------------
# Romanization + fuzzy matching (substitutes only)

_INITIALS = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
             "j", "jj", "c", "k", "t", "p", "h"]
_MEDIALS = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
            "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_FINALS = ["", "g", "kk", "gs", "n", "nj", "nh", "d", "l", "lg", "lm", "lb",
           "ls", "lt", "lp", "lh", "m", "b", "bs", "s", "ss", "ng", "j",
           "c", "k", "t", "p", "h"]


def _romanize_hangul(text: str) -> str:
    """Revised Romanization via direct Hangul syllable decomposition
    (Unicode arithmetic on the U+AC00..U+D7A3 block) — no external
    dependency. Non-Hangul characters (spaces, hyphens, Latin) pass
    through unchanged so e.g. "칼버트-르윈" romanizes hyphen and all."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code <= 11171:
            initial = code // (21 * 28)
            medial = (code % (21 * 28)) // 28
            final = code % 28
            out.append(_INITIALS[initial] + _MEDIALS[medial] + _FINALS[final])
        else:
            out.append(ch)
    return "".join(out)


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
