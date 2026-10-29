from __future__ import annotations

import asyncio
import json
import logging

from google.genai import types
from pydantic import TypeAdapter, ValidationError

from services.gemini_service import _MODEL_NAME, _make_client
from .schemas import EvidenceArticle, TrendCandidate, TrendDraft

logger = logging.getLogger(__name__)
_limit = asyncio.Semaphore(2)


async def _generate_json(prompt: str, schema: dict, retries: int = 2) -> tuple[dict | list, dict]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with _limit:
                client = _make_client()
                async with client.aio as ai:
                    async with asyncio.timeout(45):
                        response = await ai.models.generate_content(
                            model=_MODEL_NAME,
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                response_json_schema=schema,
                                temperature=0.2,
                            ),
                        )
            usage = response.usage_metadata
            return json.loads(response.text), {
                "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
                "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
                "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
            }
        except (json.JSONDecodeError, ValidationError, TimeoutError, ValueError) as exc:
            last_error = exc
            logger.warning("K-food Gemini JSON attempt %d failed: %s", attempt + 1, exc)
    raise RuntimeError("Gemini returned invalid structured data") from last_error


async def select_candidates(articles: list[EvidenceArticle]) -> tuple[list[TrendCandidate], dict]:
    indexed = [{"index": i, **article.model_dump(mode="json")} for i, article in enumerate(articles)]
    adapter = TypeAdapter(list[TrendCandidate])
    prompt = f"""Select 1-5 Korean food trend candidates from the supplied evidence.
Return only foods, desserts, drinks, or convenience-store products useful to
Japanese users of a Korean cooking app. Prefer recent evidence. Do not invent
popularity, sales, companies, or facts. Exclude restaurants, ads, coupons, and
ordinary news topics. Use evidence_indexes for supporting article indexes.

NEWS={json.dumps(indexed, ensure_ascii=False)}"""
    raw, usage = await _generate_json(prompt, adapter.json_schema())
    candidates = adapter.validate_python(raw)
    valid = []
    for candidate in candidates:
        indexes = list(dict.fromkeys(candidate.evidence_indexes))
        if candidate.suitable_for_japan and indexes and all(0 <= i < len(articles) for i in indexes):
            candidate.evidence_indexes = indexes
            valid.append(candidate)
    return valid[:5], usage


async def generate_draft(candidate: TrendCandidate, evidence: list[EvidenceArticle]) -> tuple[TrendDraft, dict]:
    prompt = f"""You are a Japanese food-app editor introducing Korean food trends.
Create a concise, natural Japanese draft for an app card and detail screen.
Lead with taste, texture, ingredients, and what makes the food enjoyable.
Do not write a news article or copy article wording, headlines, or publisher
expressions. Use only evidence-supported facts; never invent sales numbers,
rankings, companies, or claims.

Strict rules:
- description_ja: at most 2 short Japanese sentences, ideally 35-55 characters.
- trend_reason_ja: at most 3 short Japanese sentences explaining current Korean
  interest; avoid repeated wording.
- Keep sentences short and mobile-friendly. No markdown or article prose.
- Create ingredients_ja and steps_ja only when cooking is verified by evidence;
  otherwise set both fields to null.
- Never invent cooking times or difficulty; use null when unverified.
- source_urls must contain only URLs present in supplied evidence.

CANDIDATE={candidate.model_dump_json()}
EVIDENCE={json.dumps([row.model_dump(mode='json') for row in evidence], ensure_ascii=False)}"""
    raw, usage = await _generate_json(prompt, TrendDraft.model_json_schema())
    draft = TrendDraft.model_validate(raw)
    allowed_urls = {row.url for row in evidence}
    if not set(draft.source_urls).issubset(allowed_urls):
        raise ValueError("Gemini returned source URLs outside supplied evidence")
    return draft, usage
