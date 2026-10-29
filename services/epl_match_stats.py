"""EPL match intelligence: season stats, real-data-grounded prediction, key stats, form.

No LLM involved here — everything is a deterministic calculation from Naver's
official season team statistics (goals/game, xG, possession, etc). If a team
has not played enough matches yet, prediction is omitted rather than guessed.
"""
import logging
import math

import httpx
from services.epl_standings import (
    NAVER_SPORTS_TEAM_STATS_URL,
    _HEADERS,
    _fetch_current_season_code,
)
from services.epl_team_names import en_team

logger = logging.getLogger(__name__)

_MIN_MATCHES_FOR_PREDICTION = 3
_HOME_ADVANTAGE = 1.1  # standard football-analytics home-goal multiplier
_MAX_GOALS = 8  # Poisson summation cutoff

# Bump this whenever compute_prediction's formula/weights change — Supabase
# keys stored predictions by (match_id, model_version) so a version bump
# naturally produces fresh predictions instead of serving stale ones under
# a name that no longer matches how they were computed.
MODEL_VERSION = "poisson-v1"


async def fetch_season_team_stats() -> dict[str, dict]:
    """Fetch this season's per-team stats from Naver, keyed by English team name."""
    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        season_code = await _fetch_current_season_code(client)
        resp = await client.get(NAVER_SPORTS_TEAM_STATS_URL.format(season_code=season_code))
        resp.raise_for_status()
        data = resp.json()

    rows = data.get("result", {}).get("seasonTeamStats", []) or []
    stats: dict[str, dict] = {}
    for row in rows:
        team_name = en_team(row.get("teamName", ""))
        played = row.get("matchesPlayed", 0) or 0
        stats[team_name] = {
            "played": played,
            "won": row.get("wins", 0),
            "drawn": row.get("draws", 0),
            "lost": row.get("losses", 0),
            "points": row.get("points", 0),
            "rank": row.get("rank"),
            "goals_per_game": row.get("goalsPerGame"),
            "goals_conceded_per_game": row.get("goalsConcededPerGame"),
            "expected_goals": row.get("expectedGoals"),
            "expected_goals_conceded": row.get("expectedGoalsConceded"),
            "shots_per_game": row.get("shotsPerGame"),
            "possession": row.get("possession"),
            "passes_accuracy": row.get("passesAccuracy"),
            "clean_sheets": row.get("cleanSheets"),
            "form": row.get("lastFiveGames") or "",
        }
    return stats


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def compute_prediction(home: dict | None, away: dict | None) -> dict | None:
    """Poisson goal-expectancy model grounded in real season averages.

    Returns None (not a guess) if either team lacks enough real matches played.
    """
    if not home or not away:
        return None
    if home["played"] < _MIN_MATCHES_FOR_PREDICTION or away["played"] < _MIN_MATCHES_FOR_PREDICTION:
        return None

    home_gpg = home.get("goals_per_game")
    home_cpg = home.get("goals_conceded_per_game")
    away_gpg = away.get("goals_per_game")
    away_cpg = away.get("goals_conceded_per_game")
    if None in (home_gpg, home_cpg, away_gpg, away_cpg):
        return None

    # League-average goals/game proxy from the two teams themselves (avoids a
    # separate league-wide fetch while still being derived from real data).
    league_avg = (home_gpg + away_gpg + home_cpg + away_cpg) / 4 or 1.0

    home_attack = home_gpg / league_avg
    home_defense = home_cpg / league_avg
    away_attack = away_gpg / league_avg
    away_defense = away_cpg / league_avg

    exp_home_goals = home_attack * away_defense * league_avg * _HOME_ADVANTAGE
    exp_away_goals = away_attack * home_defense * league_avg
    exp_home_goals = max(0.05, exp_home_goals)
    exp_away_goals = max(0.05, exp_away_goals)

    home_win = draw = away_win = 0.0
    for h in range(_MAX_GOALS):
        for a in range(_MAX_GOALS):
            p = _poisson_pmf(h, exp_home_goals) * _poisson_pmf(a, exp_away_goals)
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p

    total = home_win + draw + away_win
    home_pct = round(home_win / total * 100)
    draw_pct = round(draw / total * 100)
    away_pct = 100 - home_pct - draw_pct

    return {
        "home": home_pct,
        "draw": draw_pct,
        "away": away_pct,
        "expected_goals": {
            "home": round(exp_home_goals, 2),
            "away": round(exp_away_goals, 2),
        },
        "sample_size": {"home_matches": home["played"], "away_matches": away["played"]},
    }


_KEY_STAT_FIELDS = [
    ("goals_per_game", "Goals / Game"),
    ("expected_goals", "xG"),
    ("shots_per_game", "Shots / Game"),
    ("possession", "Possession %"),
    ("passes_accuracy", "Pass Accuracy %"),
]


def build_key_stats(home: dict | None, away: dict | None) -> list[dict]:
    """Only include a row when BOTH teams have that real stat available."""
    if not home or not away:
        return []
    rows = []
    for field, label in _KEY_STAT_FIELDS:
        h_val = home.get(field)
        a_val = away.get(field)
        if h_val is None or a_val is None:
            continue
        rows.append({"label": label, "home": h_val, "away": a_val})
    return rows


def build_form(home: dict | None, away: dict | None) -> dict:
    return {
        "home": (home or {}).get("form", ""),
        "away": (away or {}).get("form", ""),
    }
