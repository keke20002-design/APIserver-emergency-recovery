from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from supabase import Client, create_client

TABLE = "kfood_trends"
RUNS_TABLE = "kfood_trend_runs"


class CollectionAlreadyRunning(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    return create_client(url, key)


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def food_key(title_ko: str) -> str:
    normalized = "".join(title_ko.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def recently_exists(title_ko: str, days: int = 90) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    key = food_key(title_ko)
    result = await _run(lambda: _client().table(TABLE).select("id").eq("food_key", key)
                        .gte("created_at", cutoff).limit(1).execute())
    return bool(result.data)


async def insert_draft(payload: dict) -> dict:
    values = {**payload, "food_key": food_key(payload["title_ko"]), "status": "draft"}
    result = await _run(lambda: _client().table(TABLE).insert(values).execute())
    if not result.data:
        raise RuntimeError("Failed to insert K-food trend draft")
    return result.data[0]


async def public_list(start: datetime | None, limit: int = 50) -> list[dict]:
    def query():
        request = _client().table(TABLE).select("*").eq("status", "published")
        if start:
            request = request.gte("published_at", start.isoformat())
        return request.order("published_at", desc=True).limit(limit).execute()
    return (await _run(query)).data or []


async def public_detail(trend_id: str) -> dict | None:
    result = await _run(lambda: _client().table(TABLE).select("*").eq("id", trend_id)
                        .eq("status", "published").limit(1).execute())
    return result.data[0] if result.data else None


async def admin_list(status: str | None, limit: int = 100) -> list[dict]:
    def query():
        request = _client().table(TABLE).select("*")
        if status:
            request = request.eq("status", status)
        return request.order("created_at", desc=True).limit(limit).execute()
    return (await _run(query)).data or []


async def admin_detail(trend_id: str) -> dict | None:
    result = await _run(lambda: _client().table(TABLE).select("*").eq("id", trend_id).limit(1).execute())
    return result.data[0] if result.data else None


async def update(trend_id: str, payload: dict) -> dict | None:
    values = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    result = await _run(lambda: _client().table(TABLE).update(values).eq("id", trend_id).execute())
    return result.data[0] if result.data else None


async def set_status(trend_id: str, status: str) -> dict | None:
    values = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if status == "published":
        values["published_at"] = datetime.now(timezone.utc).isoformat()
    result = await _run(lambda: _client().table(TABLE).update(values).eq("id", trend_id).execute())
    return result.data[0] if result.data else None


async def has_recent_running_job(minutes: int = 90) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    await _run(lambda: _client().table(RUNS_TABLE).update({
        "status": "failed",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": "stale running job expired",
    }).eq("status", "running").lt("started_at", cutoff).execute())
    result = await _run(lambda: _client().table(RUNS_TABLE).select("id").eq("status", "running")
                        .gte("started_at", cutoff).limit(1).execute())
    return bool(result.data)


async def start_run(trigger: str) -> str:
    try:
        result = await _run(lambda: _client().table(RUNS_TABLE).insert({
            "status": "running", "trigger": trigger,
        }).execute())
    except Exception as exc:
        if "23505" in str(exc) or "kfood_trend_one_running_idx" in str(exc):
            raise CollectionAlreadyRunning from exc
        raise
    if not result.data:
        raise RuntimeError("Failed to create K-food trend run")
    return str(result.data[0]["id"])


async def finish_run(run_id: str, status: str, metrics: dict, error: str | None = None) -> None:
    await _run(lambda: _client().table(RUNS_TABLE).update({
        "status": status,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "error": error,
    }).eq("id", run_id).execute())
