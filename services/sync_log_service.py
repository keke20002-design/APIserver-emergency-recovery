"""sync_logs 테이블 - 일일 배치(크롤링 + 마감 처리) 실행 기록."""
from __future__ import annotations

import asyncio
import os
from functools import lru_cache

from supabase import Client, create_client

TABLE = "sync_logs"


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
            "are required for sync_logs write operations"
        )
    return create_client(url, key)


def _run_sync(fn):
    return asyncio.get_event_loop().run_in_executor(None, fn)


async def record_run(
    status: str,
    notices_collected: int,
    schedules_closed: int,
    error_message: str | None = None,
) -> None:
    def _query():
        return (
            _get_admin_client()
            .table(TABLE)
            .insert(
                {
                    "status": status,
                    "notices_collected": notices_collected,
                    "schedules_closed": schedules_closed,
                    "error_message": error_message,
                }
            )
            .execute()
        )

    await _run_sync(_query)


async def recent_runs(limit: int = 20) -> list[dict]:
    def _query():
        return (
            _get_client()
            .table(TABLE)
            .select("*")
            .order("run_at", desc=True)
            .limit(limit)
            .execute()
        )

    result = await _run_sync(_query)
    return result.data or []
