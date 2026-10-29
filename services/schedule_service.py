"""locakcurapp 발행 일정(issuance_schedules) Supabase 서비스.

읽기는 anon 키(RLS로 select만 허용), 쓰기(관리자 등록/크롤러 upsert/마감
처리)는 service_role 키를 쓴다 - region_service.py와 동일한 패턴.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from supabase import Client, create_client

logger = logging.getLogger(__name__)

TABLE = "issuance_schedules"


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
            "are required for schedule write operations"
        )
    return create_client(url, key)


def _run_sync(fn):
    return asyncio.get_event_loop().run_in_executor(None, fn)


async def get_schedules(sigungu_code: str, status: str = "OPEN") -> list[dict]:
    def _query():
        return (
            _get_client()
            .table(TABLE)
            .select("*")
            .eq("sigungu_code", sigungu_code)
            .eq("status", status)
            .eq("publication_status", "PUBLISHED")
            .order("issuance_datetime")
            .execute()
        )

    result = await _run_sync(_query)
    return [_to_app_schedule(row) for row in (result.data or [])]


async def list_all_schedules(
    limit: int = 50,
    offset: int = 0,
    query: str | None = None,
    publication_status: str | None = None,
) -> list[dict]:
    """관리자 대시보드용 전체 목록 (지역 무관, 최신 등록순)."""

    def _query():
        request = _get_client().table(TABLE).select("*")
        if query:
            safe_query = query.replace(",", " ").strip()
            request = request.or_(
                f"product_name.ilike.%{safe_query}%,region_name.ilike.%{safe_query}%"
            )
        if publication_status:
            request = request.eq("publication_status", publication_status)
        return request.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

    result = await _run_sync(_query)
    return result.data or []


async def create_schedule(payload: dict) -> dict:
    """POST /admin/schedules - 운영자 수기 등록."""
    payload = {
        **payload,
        "source": payload.get("source") or "ADMIN",
        "publication_status": payload.get("publication_status") or "DRAFT",
    }
    if payload["publication_status"] == "PUBLISHED":
        payload["approved_at"] = datetime.now(timezone.utc).isoformat()

    def _query():
        return _get_admin_client().table(TABLE).insert(payload).execute()

    result = await _run_sync(_query)
    if not result.data:
        raise RuntimeError("Failed to insert schedule")
    return result.data[0]


async def update_schedule(schedule_id: int, payload: dict) -> dict | None:
    """PUT /admin/schedules/{id} - 운영자 수기 수정."""
    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    if payload.get("publication_status") == "PUBLISHED":
        payload["approved_at"] = datetime.now(timezone.utc).isoformat()

    def _query():
        return (
            _get_admin_client()
            .table(TABLE)
            .update(payload)
            .eq("id", schedule_id)
            .execute()
        )

    result = await _run_sync(_query)
    return result.data[0] if result.data else None


async def upsert_from_crawler(rows: list[dict]) -> int:
    """크롤러가 구조화까지 끝낸 행을 upsert (source + source_notice_id 기준).

    현재 크롤러는 원문만 crawled_notices에 저장하고 여기까지는 아직 안
    쓰지만, 나중에 공지 텍스트에서 구조화된 필드를 뽑아낼 수 있게 되면
    (예: gemini_service 활용) 이 함수로 issuance_schedules에 반영한다.
    """
    if not rows:
        return 0

    def _query():
        return (
            _get_admin_client()
            .table(TABLE)
            .upsert(rows, on_conflict="source,source_notice_id")
            .execute()
        )

    result = await _run_sync(_query)
    return len(result.data or [])


async def close_expired_schedules() -> int:
    """발행 후 하루가 지난 OPEN 일정을 CLOSED로 전환."""

    def _query():
        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(days=1)).isoformat()
        return (
            _get_admin_client()
            .table(TABLE)
            .update({"status": "CLOSED", "updated_at": now_dt.isoformat()})
            .eq("status", "OPEN")
            .lt("issuance_datetime", cutoff)
            .execute()
        )

    result = await _run_sync(_query)
    return len(result.data or [])


def _to_app_schedule(row: dict) -> dict:
    """DB 컬럼을 Flutter 공개 계약으로 변환한다."""
    return {
        "id": str(row["id"]),
        "region_name": row["region_name"],
        "product_name": row["product_name"],
        "issuance_datetime": row["issuance_datetime"],
        "discount_rate": float(row.get("discount_rate") or 0),
        "per_person_limit": int(row.get("per_person_limit") or 0),
        "purchase_app_name": row.get("app_name") or "",
        "live_status": "AVAILABLE" if row.get("status") == "OPEN" else "SOLD_OUT",
        "available_count": 0,
        "waiting_count": 0,
        "sold_out_count": 0,
        "app_scheme_ios": row.get("app_scheme_ios"),
        "package_name_android": row.get("package_name_android"),
        "store_url_ios": row.get("store_url_ios"),
        "store_url_android": row.get("store_url_android"),
        "web_url": row.get("web_url"),
    }
