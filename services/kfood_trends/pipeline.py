from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import repository
from .ai import generate_draft, select_candidates
from .collector import collect_recent_news

logger = logging.getLogger(__name__)
_process_lock = asyncio.Lock()


async def run_weekly_collection(trigger: str = "scheduler") -> dict:
    if _process_lock.locked() or await repository.has_recent_running_job():
        return {"status": "skipped", "reason": "collection_already_running"}
    async with _process_lock:
        try:
            run_id = await repository.start_run(trigger)
        except repository.CollectionAlreadyRunning:
            return {"status": "skipped", "reason": "collection_already_running"}
        metrics = {"naver_requests": 0, "articles": 0, "candidates": 0,
                   "gemini_requests": 0, "drafts_created": 0, "duplicates": 0,
                   "draft_failures": 0, "gemini_prompt_tokens": 0,
                   "gemini_output_tokens": 0, "gemini_total_tokens": 0}
        try:
            articles, request_count = await collect_recent_news()
            metrics.update(naver_requests=request_count, articles=len(articles))
            if not articles:
                await repository.finish_run(run_id, "success", metrics)
                return {"status": "success", "run_id": run_id, **metrics}
            candidates, usage = await select_candidates(articles)
            metrics["gemini_requests"] += 1
            _add_usage(metrics, usage)
            metrics["candidates"] = len(candidates)
            for candidate in candidates:
                if await repository.recently_exists(candidate.food_name):
                    metrics["duplicates"] += 1
                    continue
                evidence = [articles[index] for index in candidate.evidence_indexes]
                try:
                    draft, usage = await generate_draft(candidate, evidence)
                    metrics["gemini_requests"] += 1
                    _add_usage(metrics, usage)
                    payload = draft.model_dump(mode="json")
                    payload.update({
                        "evidence": [row.model_dump(mode="json") for row in evidence],
                        "mention_count": len(evidence),
                        "source_count": len({row.publisher for row in evidence}),
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    })
                    await repository.insert_draft(payload)
                    metrics["drafts_created"] += 1
                except Exception:
                    metrics["draft_failures"] += 1
                    logger.exception("K-food draft failed candidate=%s", candidate.food_name)
            await repository.finish_run(run_id, "success", metrics)
            logger.info("K-food weekly collection complete run=%s metrics=%s", run_id, metrics)
            return {"status": "success", "run_id": run_id, **metrics}
        except Exception as exc:
            await repository.finish_run(run_id, "failed", metrics, str(exc)[:2000])
            logger.exception("K-food weekly collection failed run=%s", run_id)
            raise


def _add_usage(metrics: dict, usage: dict) -> None:
    metrics["gemini_prompt_tokens"] += usage.get("prompt_tokens", 0)
    metrics["gemini_output_tokens"] += usage.get("output_tokens", 0)
    metrics["gemini_total_tokens"] += usage.get("total_tokens", 0)
