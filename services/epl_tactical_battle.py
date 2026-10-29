"""Tactical Battle: LLM analysis strictly grounded in backend-supplied match data.

The model is NEVER allowed to invent facts (formations, player roles, injuries,
manager decisions, pressing intensity, etc). It may only describe patterns
supported by the data block we hand it, and must use cautious language for
anything inferred rather than directly observed. If too little real data is
available, we skip the LLM call entirely and return an "unavailable" result —
we do not ask the model to fill gaps.

Localization: a single Gemini call generates ko/en/ja variants together and we
cache all three — switching the app's language never triggers a new request.
"""
import hashlib
import json
import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_TEAM = {"home", "away"}
_LOCALES = ("ko", "en", "ja")

# Bump this whenever the prompt or output schema changes so old cached
# records (e.g. the pre-localization English-only shape) are never read back
# under the new schema — they simply become unreachable, orphaned cache keys.
PROMPT_VERSION = "tactical-v2"

_EMPTY_LOCALIZED = {"ko": None, "en": None, "ja": None}

_EMPTY_RESULT = {
    "available": False,
    "promptVersion": PROMPT_VERSION,
    "headline": dict(_EMPTY_LOCALIZED),
    "homeFormation": None,
    "awayFormation": None,
    "keyBattle": dict(_EMPTY_LOCALIZED),
    "overloads": [],
    "tacticalTakeaway": dict(_EMPTY_LOCALIZED),
    "dataGrounding": {"ko": [], "en": [], "ja": []},
}


def _get_client() -> genai.Client:
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))


def build_match_context(
    *,
    home_team: str,
    away_team: str,
    home_formation: str | None,
    away_formation: str | None,
    home_top_players: list[dict],
    away_top_players: list[dict],
    home_stats: dict | None,
    away_stats: dict | None,
    h2h: list[dict],
) -> dict:
    """Collect ONLY the real, currently-available fields. Nothing here is invented —
    missing data is simply absent from the returned dict, not filled with a guess."""
    ctx: dict = {"home_team": home_team, "away_team": away_team}

    if home_formation:
        ctx["home_formation"] = home_formation
    if away_formation:
        ctx["away_formation"] = away_formation

    if home_top_players:
        ctx["home_top_players"] = [
            {"name": p.get("name"), "goals": p.get("goals"), "assists": p.get("assists")}
            for p in home_top_players[:3]
        ]
    if away_top_players:
        ctx["away_top_players"] = [
            {"name": p.get("name"), "goals": p.get("goals"), "assists": p.get("assists")}
            for p in away_top_players[:3]
        ]

    def _style_snapshot(stats: dict | None) -> dict | None:
        if not stats:
            return None
        snap = {}
        for key in ("possession", "shots_per_game", "passes_accuracy", "goals_per_game",
                    "goals_conceded_per_game", "form"):
            if stats.get(key) is not None and stats.get(key) != "":
                snap[key] = stats[key]
        return snap or None

    home_style = _style_snapshot(home_stats)
    away_style = _style_snapshot(away_stats)
    if home_style:
        ctx["home_style"] = home_style
    if away_style:
        ctx["away_style"] = away_style

    if h2h:
        ctx["recent_h2h"] = [
            {
                "date": g.get("date"),
                "home": g.get("home"),
                "away": g.get("away"),
                "home_score": g.get("home_score"),
                "away_score": g.get("away_score"),
            }
            for g in h2h[:5]
        ]

    return ctx


def _context_signature(ctx: dict) -> str:
    """Stable short hash of the grounding data — used as a cache key component so
    the cached analysis is only regenerated when the underlying real data changes."""
    blob = json.dumps(ctx, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def has_enough_data(ctx: dict) -> bool:
    """At least one real signal beyond team names must be present."""
    signal_keys = (
        "home_formation", "away_formation", "home_top_players", "away_top_players",
        "home_style", "away_style", "recent_h2h",
    )
    return any(ctx.get(k) for k in signal_keys)


_PROMPT_TEMPLATE = """You are a football tactical analyst. You will be given a JSON
data block containing ONLY confirmed, real data for one EPL match. You must base
your entire analysis strictly on this data.

DATA:
{data_json}

Generate all tactical analysis content in Korean, English, and Japanese.
Preserve the tactical meaning across languages — these must be genuine
translations of the SAME analysis, not different analyses per language. Do not
translate team names incorrectly: keep team names exactly as given in the data
block (e.g. "Chelsea", "Hull City") in all three languages — do not localize
or transliterate them. Use natural football terminology appropriate for each
language rather than literal word-for-word translation.

Rules (must follow exactly):
- Never write the literal words "home_team" or "away_team" in any output
  text — those are JSON keys for you to read, not labels to echo back. Always
  use the actual team name instead.
- Only use the data provided above. Do not invent formations, player roles,
  tactical systems, pressing intensity, overloads, player positioning, injuries,
  tactical instructions, or manager decisions.
- If "home_formation" or "away_formation" is missing from the data, set the
  corresponding output field to null and do not guess a formation.
- If player positioning / lineup data is insufficient to identify a specific
  overload, do not claim one. Only include an item in "overloads" when the
  supplied data actually supports it.
- For any inferred (not directly observed) point, use cautious language in
  each language (e.g. English "may look to" / "the data suggests"; natural
  equivalents in Korean and Japanese). Do not state inferred points as
  certainties.
- "confidence" on each overload must reflect how directly the data block
  supports it: "high" only if the data block explicitly names the players/stat
  behind it, "medium" for a reasonable pattern-based inference, "low" for a
  weak/speculative inference. This single confidence value applies to the
  overload itself, not per-language.
- "dataGrounding" must list, in plain language and using the real team names,
  exactly which fields from the data block you used (e.g. "Chelsea's 4-2-3-1
  formation", "Hull City's 67% average possession"), translated naturally into
  each of the three languages. Do not list anything not present in the data.
- If the data block is too sparse to say anything meaningful, return short/null
  fields rather than padding with generic filler text.
- Do not include any of the following as literal output text in any language:
  "Tactical Battle", "Key Battle", "Overload", "Tactical Takeaway" — those are
  UI section labels handled separately by the app, not analysis content.

Respond with ONLY this exact JSON shape, no other text. Every {{"ko":...,
"en":..., "ja":...}} object must have all three keys filled (never omit one):
{{
  "headline": {{"ko": string, "en": string, "ja": string}},
  "homeFormation": string|null,
  "awayFormation": string|null,
  "keyBattle": {{"ko": string, "en": string, "ja": string}} | null,
  "overloads": [
    {{
      "team": "home"|"away",
      "area": string,
      "explanation": {{"ko": string, "en": string, "ja": string}},
      "confidence": "high"|"medium"|"low"
    }}
  ],
  "tacticalTakeaway": {{"ko": string, "en": string, "ja": string}},
  "dataGrounding": {{"ko": [string], "en": [string], "ja": [string]}}
}}"""


def _sanitize_localized(raw, *, list_of_strings: bool = False):
    """Coerce a {'ko':..,'en':..,'ja':..} blob into a clean dict with all three
    keys present, falling back ja→en and ko→en for any language the model left
    out — so the client never has to handle a missing key."""
    empty = [] if list_of_strings else None
    if not isinstance(raw, dict):
        return {"ko": empty, "en": empty, "ja": empty}

    def _clean(v):
        if list_of_strings:
            return [s for s in v if isinstance(s, str)] if isinstance(v, list) else empty
        return v if isinstance(v, str) and v else None

    en = _clean(raw.get("en"))
    ko = _clean(raw.get("ko")) or en
    ja = _clean(raw.get("ja")) or en
    return {"ko": ko, "en": en, "ja": ja}


def _sanitize(parsed: dict) -> dict:
    """Defensively coerce the model's response into the strict schema, dropping
    any malformed entries rather than trusting the model blindly."""
    out = dict(_EMPTY_RESULT)
    out["available"] = True
    out["headline"] = _sanitize_localized(parsed.get("headline"))
    out["homeFormation"] = parsed.get("homeFormation") or None
    out["awayFormation"] = parsed.get("awayFormation") or None
    out["keyBattle"] = _sanitize_localized(parsed.get("keyBattle"))

    overloads = []
    for item in parsed.get("overloads") or []:
        if not isinstance(item, dict):
            continue
        team = item.get("team")
        confidence = item.get("confidence")
        if team not in _VALID_TEAM or confidence not in _VALID_CONFIDENCE:
            continue
        if not item.get("area"):
            continue
        explanation = _sanitize_localized(item.get("explanation"))
        if not explanation["en"]:
            continue
        overloads.append({
            "team": team,
            "area": item["area"],
            "explanation": explanation,
            "confidence": confidence,
        })
    out["overloads"] = overloads

    out["tacticalTakeaway"] = _sanitize_localized(parsed.get("tacticalTakeaway"))
    out["dataGrounding"] = _sanitize_localized(parsed.get("dataGrounding"), list_of_strings=True)
    return out


async def generate_tactical_battle(ctx: dict) -> dict:
    """Returns a result matching the strict localized schema, or an
    'unavailable' result (available=False) if there isn't enough real data or
    the LLM call fails. Never returns fabricated tactical facts. One call
    produces ko/en/ja together — the app never re-requests on language switch."""
    if not has_enough_data(ctx):
        logger.info("Tactical battle: insufficient data for %s vs %s, skipping LLM call",
                    ctx.get("home_team"), ctx.get("away_team"))
        return dict(_EMPTY_RESULT)

    prompt = _PROMPT_TEMPLATE.format(data_json=json.dumps(ctx, ensure_ascii=False, indent=2))

    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.4,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        parsed = json.loads(text)
        return _sanitize(parsed)
    except Exception as e:
        logger.warning("Tactical battle generation failed (%s vs %s): %s",
                        ctx.get("home_team"), ctx.get("away_team"), e)
        return dict(_EMPTY_RESULT)
