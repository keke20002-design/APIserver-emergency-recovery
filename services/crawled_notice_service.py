"""crawled_notices 테이블 - 크롤러가 모은 원문 공지 저장/조회."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from functools import lru_cache

from supabase import Client, create_client

TABLE = "crawled_notices"


@lru_cache(maxsize=1)
def _get_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY environment variables are required"
        )
    return create_client(url, key)


@lru_cache(maxsize=1)
def _get_admin_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables "
            "are required for crawled_notices write operations"
        )
    return create_client(url, key)


def _run_sync(fn):
    return asyncio.get_event_loop().run_in_executor(None, fn)


async def upsert_notices(notices: list[dict]) -> int:
    if not notices:
        return 0

    def _query():
        return (
            _get_admin_client()
            .table(TABLE)
            .upsert(notices, on_conflict="platform,notice_id")
            .execute()
        )

    result = await _run_sync(_query)
    return len(result.data or [])


async def list_unreviewed(limit: int = 50) -> list[dict]:
    def _query():
        return (
            _get_client()
            .table(TABLE)
            .select("*")
            .eq("reviewed", False)
            .order("fetched_at", desc=True)
            .limit(limit)
            .execute()
        )

    result = await _run_sync(_query)
    return result.data or []


async def mark_reviewed(notice_id: int, linked_schedule_id: int | None = None) -> dict | None:
    changes = {"reviewed": True, "reviewed_at": datetime.now(timezone.utc).isoformat()}
    if linked_schedule_id is not None:
        changes["linked_schedule_id"] = linked_schedule_id

    def _query():
        return (
            _get_admin_client()
            .table(TABLE)
            .update(changes)
            .eq("id", notice_id)
            .execute()
        )

    result = await _run_sync(_query)
    return result.data[0] if result.data else None


async def get_notice(notice_id: int) -> dict | None:
    def _query():
        return _get_client().table(TABLE).select("*").eq("id", notice_id).limit(1).execute()

    result = await _run_sync(_query)
    return result.data[0] if result.data else None
