"""지역화폐 앱(locakcurapp) - 법정동코드 기반 지역 마스터 데이터.

regions 테이블(sido_code/sido_name/sigungu_code/sigungu_name/is_active)을
Supabase에서 읽고 검색한다. 테이블 DDL은
services/region_service.sql 참고 - Supabase SQL Editor에서 직접 실행 필요
(Python 클라이언트로는 DDL을 실행할 수 없음).
"""
from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache

from supabase import Client, create_client

logger = logging.getLogger(__name__)

TABLE = "regions"


@lru_cache(maxsize=1)
def _get_client() -> Client:
    """읽기 전용 클라이언트 (anon/publishable 키, RLS 적용됨)."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY environment variables are required"
        )
    return create_client(url, key)


@lru_cache(maxsize=1)
def _get_admin_client() -> Client:
    """쓰기(시딩)용 클라이언트 (service_role 키, RLS 우회).

    regions는 anon에게 select만 허용하는 RLS 정책이 있어 (region_service.sql)
    upsert 같은 쓰기 작업은 이 클라이언트로만 가능하다.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables "
            "are required for region write operations"
        )
    return create_client(url, key)


def _run_sync(fn):
    return asyncio.get_event_loop().run_in_executor(None, fn)


async def _fetch_active_rows() -> list[dict]:
    def _query():
        return (
            _get_client()
            .table(TABLE)
            .select("*")
            .eq("is_active", True)
            .order("sido_code")
            .order("sigungu_code")
            .execute()
        )

    result = await _run_sync(_query)
    return result.data or []


async def get_hierarchy() -> list[dict]:
    """시/도 -> 시/군/구 계층 구조로 묶어서 반환."""
    rows = await _fetch_active_rows()

    grouped: dict[str, dict] = {}
    for row in rows:
        sido_code = row["sido_code"]
        bucket = grouped.setdefault(
            sido_code,
            {
                "sido_code": sido_code,
                "sido_name": row["sido_name"],
                "sigungu_list": [],
            },
        )
        bucket["sigungu_list"].append(
            {
                "sigungu_code": row["sigungu_code"],
                "sigungu_name": row["sigungu_name"],
                "has_active_voucher": bool(row.get("is_active", True)),
            }
        )
    return list(grouped.values())


async def search(query: str, limit: int = 20) -> list[dict]:
    """지역명 부분 검색. 데이터가 소규모(수백 건)라 전체를 읽어 메모리에서
    필터링한다 - PostgREST 필터 문자열에 사용자 입력을 직접 넣지 않기 위함."""
    normalized = query.strip().lower()
    if not normalized:
        return []
    rows = await _fetch_active_rows()
    matched = [
        row
        for row in rows
        if normalized in row["sigungu_name"].lower()
        or normalized in row["sido_name"].lower()
    ]
    return matched[:limit]


async def upsert_regions(rows: list[dict]) -> int:
    """지역 마스터 데이터 시딩/갱신용. sigungu_code 기준 upsert."""
    def _query():
        return (
            _get_admin_client()
            .table(TABLE)
            .upsert(rows, on_conflict="sigungu_code")
            .execute()
        )

    result = await _run_sync(_query)
    return len(result.data or [])
