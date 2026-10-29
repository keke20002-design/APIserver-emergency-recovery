"""Tactical Stories: short, real-data-grounded tactical *cards* per match.

Reuses the same grounding context as Tactical Battle (services.epl_tactical_battle)
so we don't build match context twice. One Gemini call returns 0-3 stories —
the model is never forced to fill a quota with filler content, and it is never
allowed to invent player coordinates/positions — that data simply isn't part
of this feature. If/when real lineup coordinates become available from the
data provider, a separate formation-board feature can consume them directly
without going through the LLM at all.

Each story is a short scannable "tactical analysis card" (title, key point,
stat comparison, why-it-matters, watch-for) rather than a news-article-style
paragraph dump — deliberately distinct from a sports article.

Localization: same approach as Tactical Battle — one call generates ko/en/ja
together, cached together, so switching the app's language never re-fetches.
"""
import json
import logging

from google.genai import types
from services.epl_tactical_battle import _get_client, has_enough_data, _sanitize_localized

logger = logging.getLogger(__name__)

_MODEL_NAME = "gemini-2.5-flash"
_VALID_TYPES = {"tactical_battle", "key_player", "space"}
_VALID_LEVELS = {"beginner", "intermediate", "advanced"}
_WORDS_PER_MINUTE = 200

# Same 3 season-stat fields shown on the card's stat-comparison table, reusing
# the exact raw labels the client already maps to localized text for the
# match-detail Key Stats section (see key_stat_format.dart) — one label
# vocabulary shared across both features.
_STAT_FIELDS = [
    ("possession", "Possession %"),
    ("passes_accuracy", "Pass Accuracy %"),
    ("shots_per_game", "Shots / Game"),
]

# Bump when the prompt/schema changes so old cached records never get read
# back under a new shape. v3 replaces the free-form summary/content article
# fields with a structured tactical-card shape (key point / stats / why it
# matters / watch for).
PROMPT_VERSION = "stories-v3"


_PROMPT_TEMPLATE = """You are a football tactical writer building short, scannable
tactical-analysis CARDS for a mobile app — NOT a news article. You will be given a
JSON data block containing ONLY confirmed, real data for one EPL match. Base
everything strictly on this data.

DATA:
{data_json}

Generate all story content in Korean, English, and Japanese. Preserve the
tactical meaning across languages — these must be genuine translations of the
SAME analysis, not different analyses per language. Keep team/player names
exactly as given in the data (e.g. "Chelsea", "Bukayo Saka") in all three
languages — natural football-media transliteration (e.g. Korean "첼시" for
Chelsea) is fine, but do not invent alternate names. Use natural football
terminology appropriate for each language rather than literal word-for-word
translation.

Generate between 1 and 3 stories, chosen ONLY from these types:
- "tactical_battle": how the two teams' styles/approaches may clash
- "key_player": a specific player whose real stats (goals/assists/form) make
  them central to this match
- "space": where on the pitch one team may find or concede space, based on
  the real possession/shots/defensive numbers in the data

Every story is a short CARD, not an article. Every field must be SHORT:
- "title": a short question or punchy phrase (max ~8 words), curiosity-driving,
  NOT a generic label like "Tactical Battle" — e.g. "Who controls midfield?"
- "keyPointHeading": an even shorter phrase (max ~6 words) naming the core
  tension, e.g. "City's possession vs United's transitions"
- "keyPointBody": exactly 1-2 short sentences expanding on keyPointHeading
- "statsInsight": exactly ONE short sentence interpreting the numeric
  comparison in "home_style"/"away_style" from the data block. Only write a
  real sentence if the data block actually contains home_style/away_style —
  otherwise return an empty string ""
- "whyItMatters": exactly TWO short paragraphs separated by a single newline
  character (\\n), each 1-3 sentences, explaining why this tactical point
  matters for how the match may play out
- "watchFor": ONE short, concrete, actionable sentence telling the fan exactly
  what to watch for during the match (a specific in-game moment or pattern) —
  this should be the single most useful, re-readable line of the card

Rules (must follow exactly):
- Do NOT force 3 stories. If the data only clearly supports 1 or 2 genuinely
  interesting angles, return only those. A generic, could-apply-to-any-match
  sentence is worse than one fewer story.
- Only include "key_player" if a specific named player's real stats (from
  home_top_players/away_top_players) justify it. Do not invent a player.
- Never invent player positions, on-pitch coordinates, formations, statistics,
  or tactical facts not present in the data block above. Do not output any
  x/y coordinates or pitch position data — this feature does not use them.
- Never write the literal words "home_team" or "away_team" in any output text
  in any language — those are JSON keys for you to read, not labels to echo.
- Use cautious language for anything inferred rather than directly observed,
  in each language ("may look to", "the data suggests", and natural
  equivalents in Korean/Japanese).
- "level" should reflect how much tactical background knowledge the story
  assumes: "beginner" (accessible to a casual fan), "intermediate", or
  "advanced". This is a single value, not per-language.
- Every number you reference in "statsInsight" or "whyItMatters" MUST come
  directly from the data block — never invent or round a statistic that
  isn't there.

Respond with ONLY this exact JSON shape, no other text. Every
{{"ko":..., "en":..., "ja":...}} object must have all three keys filled:
{{
  "stories": [
    {{
      "type": "tactical_battle"|"key_player"|"space",
      "level": "beginner"|"intermediate"|"advanced",
      "title": {{"ko": string, "en": string, "ja": string}},
      "keyPointHeading": {{"ko": string, "en": string, "ja": string}},
      "keyPointBody": {{"ko": string, "en": string, "ja": string}},
      "statsInsight": {{"ko": string, "en": string, "ja": string}},
      "whyItMatters": {{"ko": string, "en": string, "ja": string}},
      "watchFor": {{"ko": string, "en": string, "ja": string}}
    }}
  ]
}}"""


def _read_time_minutes(combined_en: str) -> int:
    words = len(combined_en.split())
    return max(1, round(words / _WORDS_PER_MINUTE)) if words else 1


def _build_story_stats(ctx: dict) -> list[dict]:
    """Deterministic (non-AI) stat comparison table, grounded in the same
    home_style/away_style numbers already handed to the model — only included
    when both teams actually have the real figure, never fabricated."""
    home_style = ctx.get("home_style") or {}
    away_style = ctx.get("away_style") or {}
    rows = []
    for field, label in _STAT_FIELDS:
        h_val = home_style.get(field)
        a_val = away_style.get(field)
        if h_val is None or a_val is None:
            continue
        rows.append({"label": label, "home": h_val, "away": a_val})
    return rows


def _sanitize_stories(parsed: dict, ctx: dict, game_id: str) -> list[dict]:
    stats = _build_story_stats(ctx)
    stories = []
    seen_types = set()
    for item in (parsed.get("stories") or [])[:3]:
        if not isinstance(item, dict):
            continue
        story_type = item.get("type")
        level = item.get("level")
        if story_type not in _VALID_TYPES or story_type in seen_types:
            continue
        if level not in _VALID_LEVELS:
            continue

        title = _sanitize_localized(item.get("title"))
        key_point_heading = _sanitize_localized(item.get("keyPointHeading"))
        key_point_body = _sanitize_localized(item.get("keyPointBody"))
        stats_insight = _sanitize_localized(item.get("statsInsight"))
        why_it_matters = _sanitize_localized(item.get("whyItMatters"))
        watch_for = _sanitize_localized(item.get("watchFor"))
        if not title["en"] or not key_point_body["en"] or not why_it_matters["en"] or not watch_for["en"]:
            continue

        seen_types.add(story_type)
        combined_en = " ".join(filter(None, [
            title["en"], key_point_body["en"], why_it_matters["en"], watch_for["en"],
        ]))
        stories.append({
            "id": f"{game_id}_{story_type}",
            "matchId": game_id,
            "type": story_type,
            "level": level,
            "title": title,
            "keyPointHeading": key_point_heading,
            "keyPointBody": key_point_body,
            "stats": stats,
            "statsInsight": stats_insight,
            "whyItMatters": why_it_matters,
            "watchFor": watch_for,
            "readTimeMinutes": _read_time_minutes(combined_en),
        })
    return stories


async def generate_tactical_stories(ctx: dict, game_id: str) -> list[dict]:
    """Returns 0-3 real-data-grounded tactical cards, each with ko/en/ja text.
    Never fabricates content when the underlying match data is too sparse —
    returns an empty list instead. One call covers all three languages."""
    if not has_enough_data(ctx):
        logger.info("Tactical stories: insufficient data for %s vs %s, skipping LLM call",
                    ctx.get("home_team"), ctx.get("away_team"))
        return []

    prompt = _PROMPT_TEMPLATE.format(data_json=json.dumps(ctx, ensure_ascii=False, indent=2))

    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.5,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        parsed = json.loads(text)
        return _sanitize_stories(parsed, ctx, game_id)
    except Exception as e:
        logger.warning("Tactical stories generation failed (%s vs %s): %s",
                        ctx.get("home_team"), ctx.get("away_team"), e)
        return []
