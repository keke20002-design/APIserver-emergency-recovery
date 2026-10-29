"""K-Food server-side AI routes. Never return provider credentials."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from google.genai import types, errors
from services.gemini_service import _make_client, _MODEL_NAME
from utils.cache import cache

router = APIRouter(prefix="/ai")
_limit = asyncio.Semaphore(3)
_inflight: dict[str, asyncio.Task] = {}
_usage_lock = asyncio.Lock()
_FREE_LIMIT = 2
_BONUS_LIMIT = 2
_RECIPE_INSTRUCTION = (
    'Answer the cooking question for the named Korean recipe in the requested locale. '
    'Use the supplied current ingredients as context. Do not invent exact recipe quantities or steps. '
    'Be concise, use no more than 100 words, and acknowledge uncertainty. '
    'Return JSON {"answer": "text"}.'
)


class RecipeQuestion(BaseModel):
    recipe_title: str = Field(min_length=1, max_length=200)
    recipe_category: str = Field(default="", max_length=100)
    current_ingredients: list[str] = Field(default_factory=list, max_length=30)
    question: str = Field(min_length=1, max_length=2000)
    locale: str = Field(default="en", pattern="^(ko|en|ja|vi|th)$")


class Substitutes(BaseModel):
    ingredient: str = Field(min_length=1, max_length=200)
    country_code: str = Field(min_length=2, max_length=10)
    country_name: str = Field(min_length=1, max_length=100)


def _usage_key(user_id: str) -> str:
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).date().isoformat()
    safe_id = hashlib.sha256(user_id.encode()).hexdigest()
    return f"kfood_ai_usage:{today}:{safe_id}"


def _read_usage(user_id: str) -> dict:
    return cache.get(_usage_key(user_id)) or {
        "free_used": 0,
        "bonus_granted": 0,
        "bonus_used": 0,
        "free_limit": _FREE_LIMIT,
        "bonus_limit": _BONUS_LIMIT,
    }


def _usage_response(usage: dict) -> dict:
    return {
        **usage,
        "free_remaining": max(0, usage["free_limit"] - usage["free_used"]),
        "total_remaining": max(
            0,
            usage["free_limit"] - usage["free_used"]
            + usage["bonus_granted"] - usage["bonus_used"],
        ),
    }


async def _reserve_question(user_id: str) -> tuple[str, dict]:
    async with _usage_lock:
        usage = _read_usage(user_id)
        if usage["free_used"] < usage["free_limit"]:
            usage["free_used"] += 1
            bucket = "free"
        elif usage["bonus_used"] < usage["bonus_granted"]:
            usage["bonus_used"] += 1
            bucket = "bonus"
        else:
            raise HTTPException(
                status_code=429,
                detail="AI_DAILY_LIMIT",
                headers={"X-AI-Remaining": "0"},
            )
        cache.set(_usage_key(user_id), usage, ttl=172800)
        return bucket, usage


async def _rollback_question(user_id: str, bucket: str) -> None:
    async with _usage_lock:
        usage = _read_usage(user_id)
        usage[f"{bucket}_used"] = max(0, usage[f"{bucket}_used"] - 1)
        cache.set(_usage_key(user_id), usage, ttl=172800)


@router.get("/recipe/usage")
async def recipe_ai_usage(
    user_id: str = Header("anonymous", alias="X-KFood-User"),
):
    return _usage_response(_read_usage(user_id[:128]))


@router.post("/recipe/reward")
async def recipe_ai_reward(
    user_id: str = Header("anonymous", alias="X-KFood-User"),
):
    async with _usage_lock:
        user_id = user_id[:128]
        usage = _read_usage(user_id)
        if usage["bonus_granted"] >= usage["bonus_limit"]:
            return {"granted": False, **_usage_response(usage)}
        usage["bonus_granted"] += 1
        cache.set(_usage_key(user_id), usage, ttl=172800)
        return {"granted": True, **_usage_response(usage)}


async def generate(payload: dict, instruction: str) -> dict:
    key = instruction + json.dumps(payload, sort_keys=True)
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_generate(payload, instruction))
        _inflight[key] = task
        def done(completed):
            _inflight.pop(key, None)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(done)
    return await asyncio.shield(task)


def _generation_cache_key(payload: dict, instruction: str) -> str:
    return "kfood_ai_v1:" + hashlib.sha256(
        (instruction + json.dumps(payload, sort_keys=True)).encode()
    ).hexdigest()


def _cached_generation(payload: dict, instruction: str) -> dict | None:
    cached = cache.get(_generation_cache_key(payload, instruction))
    return cached if isinstance(cached, dict) else None


async def _generate(payload: dict, instruction: str) -> dict:
    key = _generation_cache_key(payload, instruction)
    cached = cache.get(key)
    if cached is not None:
        return cached
    async with _limit:
        try:
            client = _make_client()
            async with client.aio as ai:
                async with asyncio.timeout(25):
                    for attempt in range(2):
                        try:
                            response = await ai.models.generate_content(
                                model=_MODEL_NAME,
                                contents=json.dumps(payload, ensure_ascii=False),
                                config=types.GenerateContentConfig(
                                    system_instruction=instruction,
                                    response_mime_type="application/json",
                                ),
                            )
                            break
                        except errors.APIError as exc:
                            if exc.code != 429 or attempt:
                                raise
                            await asyncio.sleep(1.5)
            result = json.loads(response.text)
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            cache.set(key, result, ttl=86400)
            return result
        except Exception as exc:
            raise HTTPException(status_code=502, detail="AI request failed") from exc


@router.post("/recipe/ask")
async def ask_recipe(
    request: RecipeQuestion,
    user_id: str = Header("anonymous", alias="X-KFood-User"),
):
    user_id = user_id[:128]
    payload = request.model_dump()
    # Cached answers do not call Gemini and therefore never consume a credit.
    cached = _cached_generation(payload, _RECIPE_INSTRUCTION)
    if cached is not None and isinstance(cached.get("answer"), str) and cached["answer"].strip():
        return {"answer": cached["answer"]}

    bucket, _ = await _reserve_question(user_id)
    try:
        # Recheck after reserving to avoid charging a request that raced with
        # another request which populated the same cache entry.
        cached = _cached_generation(payload, _RECIPE_INSTRUCTION)
        if cached is not None and isinstance(cached.get("answer"), str) and cached["answer"].strip():
            await _rollback_question(user_id, bucket)
            return {"answer": cached["answer"]}
        result = await generate(payload, _RECIPE_INSTRUCTION)
        if not isinstance(result.get("answer"), str) or not result["answer"].strip():
            raise HTTPException(status_code=502, detail="Empty AI answer")
    except Exception:
        await _rollback_question(user_id, bucket)
        raise
    return {"answer": result["answer"]}


@router.post("/ingredient-substitutes")
async def ingredient_substitutes(request: Substitutes):
    result = await generate(request.model_dump(),
        'Suggest locally purchasable substitutes for the Korean ingredient in the requested country. '
        'Return English JSON {"substitutes": [{"substitute": "name", '
        '"similarity_tier": "Very Similar / Similar / Acceptable / Emergency Option", '
        '"buy_difficulty": "Easy / Medium / Hard", "notes": "flavor difference", '
        '"supermarket_hint": "store hint"}]}. Pick one allowed value for each tier.')
    rows = result.get("substitutes")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("substitute"), str)
        for row in rows
    ):
        raise HTTPException(status_code=502, detail="Invalid AI substitutes")
    return {"substitutes": rows}
