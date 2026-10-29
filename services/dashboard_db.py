"""App Monetization Dashboard - Supabase 영속 계층.

기존 services/supabase_service.py와 동일한 패턴(동기 supabase-py 클라이언트를
스레드 풀에서 실행)을 재사용한다. 클라이언트는 하나만 생성해서 공유한다.

USE_MOCK_DATA=true 인 동안에는 이 모듈의 조회 함수들이 호출되지 않는다
(dashboard_router.py가 mock 경로로 분기한다). Sync 함수(upsert_*)는
Phase 3~5(Google Ads/AdMob/GA4 연동)에서 사용된다.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import date, datetime, timezone
from functools import lru_cache

from supabase import Client, create_client

logger = logging.getLogger(__name__)

GOOGLE_ADS_TABLE = "google_ads_daily"
ADMOB_TABLE = "admob_daily"
ANALYTICS_TABLE = "analytics_daily"
APPS_TABLE = "apps"
COUNTRIES_TABLE = "countries"
CAMPAIGNS_TABLE = "campaigns"
SETTINGS_TABLE = "dashboard_settings"
SYNC_STATUS_TABLE = "dashboard_sync_status"

SETTINGS_ROW_ID = 1  # dashboard_settings는 단일 row(id=1)만 사용


@lru_cache(maxsize=1)
def _get_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables are required")
    return create_client(url, key)


def _run_sync(fn):
    return asyncio.get_event_loop().run_in_executor(None, fn)


def _apply_filters(query, *, app_id=None, country=None, campaign_id=None, ad_unit_id=None):
    if app_id:
        query = query.eq("app_id", app_id)
    if country:
        query = query.eq("country", country)
    if campaign_id:
        query = query.eq("campaign_id", campaign_id)
    if ad_unit_id:
        query = query.eq("ad_unit_id", ad_unit_id)
    return query


async def get_apps() -> list[dict]:
    try:
        result = await _run_sync(lambda: _get_client().table(APPS_TABLE).select("*").order("app_name").execute())
        return result.data or []
    except Exception as e:
        logger.error("dashboard get_apps failed: %s", e)
        return []


async def upsert_apps_catalog(rows: list[dict]) -> int:
    """app_id/app_name만 upsert. package_name/active는 절대 건드리지 않는다 (관리자가 채운 값 보존)."""
    if not rows:
        return 0
    try:
        result = await _run_sync(
            lambda: _get_client().table(APPS_TABLE).upsert(rows, on_conflict="app_id").execute()
        )
        return len(result.data or [])
    except Exception as e:
        logger.error("upsert_apps_catalog failed: %s", e)
        return 0


async def create_app(data: dict) -> dict:
    """관리자가 App Mapping 화면에서 새 앱을 수동 등록. 실패 시 예외를 그대로 올린다(FK/unique 위반 등)."""
    result = await _run_sync(lambda: _get_client().table(APPS_TABLE).insert(data).execute())
    if not result.data:
        raise RuntimeError("insert returned no data")
    return result.data[0]


async def update_app(app_id: str, patch: dict) -> dict | None:
    """관리자가 App Mapping 화면에서 앱 정보(패키지명 등)를 수정. 실패 시 예외를 그대로 올린다."""
    result = await _run_sync(
        lambda: _get_client().table(APPS_TABLE).update(patch).eq("app_id", app_id).execute()
    )
    return result.data[0] if result.data else None


async def get_countries() -> list[dict]:
    try:
        result = await _run_sync(lambda: _get_client().table(COUNTRIES_TABLE).select("*").order("country_name").execute())
        return result.data or []
    except Exception as e:
        logger.error("dashboard get_countries failed: %s", e)
        return []


async def get_campaigns(app_id: str | None = None) -> list[dict]:
    try:
        def _query():
            q = _get_client().table(CAMPAIGNS_TABLE).select("*")
            if app_id:
                q = q.eq("app_id", app_id)
            return q.execute()

        result = await _run_sync(_query)
        return result.data or []
    except Exception as e:
        logger.error("dashboard get_campaigns failed: %s", e)
        return []


def _merge_campaign_catalog(existing_rows: list[dict], rows: list[dict], now: str) -> list[dict]:
    """동기화 값과 기존 수동 매핑/변경시각을 안전하게 병합한다."""
    existing = {row["campaign_id"]: row for row in existing_rows}
    payloads = []
    settings_fields = (
        "app_id", "active", "target_cpi", "daily_budget",
        "bidding_strategy_goal_type", "target_cpi_changed_at", "settings_synced_at",
    )
    for row in rows:
        previous = existing.get(row["campaign_id"], {})
        payload = {
            "campaign_id": row["campaign_id"],
            "campaign_name": row["campaign_name"],
            **{key: previous.get(key) for key in settings_fields if key in previous},
        }
        payload.update(row)
        if "target_cpi" in row:
            old_target = previous.get("target_cpi")
            new_target = row.get("target_cpi")
            old_number = float(old_target) if old_target is not None else None
            new_number = float(new_target) if new_target is not None else None
            first_observation = not previous.get("settings_synced_at")
            if first_observation:
                # Google Ads의 과거 변경일은 현재 설정 조회만으로 알 수 없다.
                payload["target_cpi_changed_at"] = previous.get("target_cpi_changed_at")
            # 원화 계정의 목표를 USD로 환산해 저장하므로 환율 변동만으로도
            # 숫자가 매일 달라진다. 작은 환산 차이를 실제 입찰 수정으로 기록하지 않는다.
            elif (old_number is None) != (new_number is None) or (
                old_number is not None and new_number is not None
                and not math.isclose(old_number, new_number, rel_tol=0.02)
            ):
                payload["target_cpi_changed_at"] = now
            else:
                payload["target_cpi_changed_at"] = previous.get("target_cpi_changed_at")
            payload["settings_synced_at"] = now
        payloads.append(payload)
    return payloads


async def upsert_campaigns_catalog(rows: list[dict]) -> int:
    """campaign_id/campaign_name만 upsert. app_id는 절대 건드리지 않는다 (관리자가 수동 매핑한 값 보존)."""
    if not rows:
        return 0
    try:
        existing = await get_campaigns()
        now = datetime.now(timezone.utc).isoformat()
        payloads = _merge_campaign_catalog(existing, rows, now)

        result = await _run_sync(
            lambda: _get_client().table(CAMPAIGNS_TABLE).upsert(payloads, on_conflict="campaign_id").execute()
        )
        return len(result.data or [])
    except Exception as e:
        logger.error("upsert_campaigns_catalog failed: %s", e)
        return 0


async def update_campaign(campaign_id: str, patch: dict) -> dict | None:
    """캠페인 매핑 변경을 과거 일별 성과에도 반영한다."""
    result = await _run_sync(
        lambda: _get_client().table(CAMPAIGNS_TABLE).update(patch).eq("campaign_id", campaign_id).execute()
    )
    if not result.data:
        return None
    if "app_id" in patch:
        # 집계는 google_ads_daily.app_id로 계산한다. 카탈로그만 바꾸면 과거
        # 광고비가 이전 앱에 남아 있고 새 앱에는 계속 0으로 표시된다.
        await _run_sync(
            lambda: _get_client().table(GOOGLE_ADS_TABLE)
            .update({"app_id": patch["app_id"]})
            .eq("campaign_id", campaign_id)
            .execute()
        )
    return result.data[0]


async def get_google_ads_daily(start: date, end: date, **filters) -> list[dict]:
    try:
        def _query():
            # PostgREST/Supabase는 기본적으로 최대 1,000행만 반환한다. 기간이 길면
            # 일부 캠페인만 잘려 30일 합계가 14일보다 작아질 수 있으므로 전 페이지를 읽는다.
            page_size = 1000
            offset = 0
            rows = []
            while True:
                q = (
                    _get_client()
                    .table(GOOGLE_ADS_TABLE)
                    .select("*")
                    .gte("date", start.isoformat())
                    .lte("date", end.isoformat())
                    .order("date")
                    .order("campaign_id")
                    .order("country")
                )
                page = _apply_filters(q, **filters).range(offset, offset + page_size - 1).execute().data or []
                rows.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
            return rows

        return await _run_sync(_query)
    except Exception as e:
        logger.error("get_google_ads_daily failed: %s", e)
        return []


async def upsert_google_ads_daily(rows: list[dict]) -> int:
    """UNIQUE(date, campaign_id, country) 기준 upsert."""
    if not rows:
        return 0
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(GOOGLE_ADS_TABLE)
            .upsert(rows, on_conflict="date,campaign_id,country")
            .execute()
        )
        return len(result.data or [])
    except Exception as e:
        logger.error("upsert_google_ads_daily failed: %s", e)
        return 0


async def get_admob_daily(start: date, end: date, **filters) -> list[dict]:
    try:
        def _query():
            q = (
                _get_client()
                .table(ADMOB_TABLE)
                .select("*")
                .gte("date", start.isoformat())
                .lte("date", end.isoformat())
            )
            return _apply_filters(q, **filters).execute()

        result = await _run_sync(_query)
        return result.data or []
    except Exception as e:
        logger.error("get_admob_daily failed: %s", e)
        return []


async def upsert_admob_daily(rows: list[dict]) -> int:
    """UNIQUE(date, app_id, ad_unit_id, country) 기준 upsert."""
    if not rows:
        return 0
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(ADMOB_TABLE)
            .upsert(rows, on_conflict="date,app_id,ad_unit_id,country")
            .execute()
        )
        return len(result.data or [])
    except Exception as e:
        logger.error("upsert_admob_daily failed: %s", e)
        return 0


async def get_analytics_daily(start: date, end: date, **filters) -> list[dict]:
    try:
        def _query():
            q = (
                _get_client()
                .table(ANALYTICS_TABLE)
                .select("*")
                .gte("date", start.isoformat())
                .lte("date", end.isoformat())
            )
            return _apply_filters(q, **filters).execute()

        result = await _run_sync(_query)
        return result.data or []
    except Exception as e:
        logger.error("get_analytics_daily failed: %s", e)
        return []


async def upsert_analytics_daily(rows: list[dict]) -> int:
    """UNIQUE(date, app_id, country) 기준 upsert."""
    if not rows:
        return 0
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(ANALYTICS_TABLE)
            .upsert(rows, on_conflict="date,app_id,country")
            .execute()
        )
        return len(result.data or [])
    except Exception as e:
        logger.error("upsert_analytics_daily failed: %s", e)
        return 0


DEFAULT_SETTINGS = {
    "id": SETTINGS_ROW_ID,
    "base_currency": "USD",
    "timezone": "Asia/Seoul",
    "target_roas_pct": 100.0,
    "roas_scale_min": 120.0,
    "roas_watch_min": 80.0,
    "roas_optimize_min": 50.0,
}


async def get_settings() -> dict:
    try:
        result = await _run_sync(
            lambda: _get_client().table(SETTINGS_TABLE).select("*").eq("id", SETTINGS_ROW_ID).limit(1).execute()
        )
        if result.data:
            return result.data[0]
        return dict(DEFAULT_SETTINGS)
    except Exception as e:
        logger.warning("get_settings failed, falling back to defaults: %s", e)
        return dict(DEFAULT_SETTINGS)


async def update_settings(patch: dict) -> dict:
    payload = {**patch, "id": SETTINGS_ROW_ID}
    try:
        result = await _run_sync(
            lambda: _get_client().table(SETTINGS_TABLE).upsert(payload).execute()
        )
        return result.data[0] if result.data else payload
    except Exception as e:
        logger.error("update_settings failed: %s", e)
        raise


async def get_sync_status() -> list[dict]:
    try:
        result = await _run_sync(lambda: _get_client().table(SYNC_STATUS_TABLE).select("*").execute())
        return result.data or []
    except Exception as e:
        logger.warning("get_sync_status failed: %s", e)
        return []


async def record_sync_status(source: str, status: str, message: str | None = None) -> None:
    """source: google_ads | admob | analytics"""
    payload = {
        "source": source,
        "status": status,
        "message": message,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await _run_sync(
            lambda: _get_client().table(SYNC_STATUS_TABLE).upsert(payload, on_conflict="source").execute()
        )
    except Exception as e:
        logger.error("record_sync_status failed for %s: %s", source, e)
