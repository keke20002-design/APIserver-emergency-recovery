"""지역화폐 알리미 앱(locakcurapp) 전용 엔드포인트.

main.py의 기존 스타일(개별 @app.get/@app.post)과 달리 이 기능만
APIRouter로 묶어 main.py에는 include_router 한 줄만 추가한다
(dashboard_router와 동일한 패턴).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from services import (
    crawled_notice_service,
    locakcur_admin_page,
    merchant_service,
    notice_crawler_service,
    region_service,
    schedule_service,
    schedule_sync_job,
    sync_log_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["locakcurapp"])
admin_router = APIRouter(prefix="/admin", tags=["locakcurapp-admin"])

_VALID_CATEGORIES = {"ALL", "RESTAURANT", "CAFE", "PHARMACY", "MART"}


class ScheduleInput(BaseModel):
    sigungu_code: str = Field(pattern=r"^\d{5}$")
    region_name: str = Field(min_length=2, max_length=100)
    product_name: str = Field(min_length=1, max_length=200)
    issuance_datetime: datetime
    discount_rate: float = Field(default=0, ge=0, le=100)
    per_person_limit: int = Field(default=0, ge=0, le=100_000_000)
    app_name: str | None = Field(default=None, max_length=100)
    package_name_android: str | None = Field(default=None, max_length=200)
    web_url: str | None = Field(default=None, max_length=2000)
    status: str = Field(default="OPEN", pattern=r"^(OPEN|CLOSED)$")
    publication_status: str = Field(default="DRAFT", pattern=r"^(DRAFT|PUBLISHED|ARCHIVED)$")

    @field_validator("issuance_datetime")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issuance_datetime must include timezone")
        return value

    @field_validator("web_url")
    @classmethod
    def validate_web_url(cls, value: str | None) -> str | None:
        if value and not value.startswith("https://"):
            raise ValueError("web_url must start with https://")
        return value


def _require_admin(admin_id: str = "", admin_pw: str = "") -> None:
    """블로그 관리자와 동일한 계정(BLOG_ADMIN_ID/PW)을 재사용한다."""
    if admin_id != os.getenv("BLOG_ADMIN_ID", "") or admin_pw != os.getenv("BLOG_ADMIN_PW", ""):
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")


@router.get("/regions/hierarchy")
async def get_regions_hierarchy():
    data = await region_service.get_hierarchy()
    return {"status": "success", "data": data}


@router.get("/regions/search")
async def search_regions(q: str = Query(..., min_length=1, max_length=50)):
    data = await region_service.search(q)
    return {"status": "success", "data": data}


@router.get("/merchants/nearby")
async def get_nearby_merchants(
    lat: float,
    lng: float,
    category: str = "ALL",
    radius: int = Query(1000, ge=100, le=3000),
):
    category = category.upper()
    if category not in _VALID_CATEGORIES:
        raise HTTPException(400, f"invalid category: {category}")
    data = await merchant_service.fetch_nearby_merchants(
        lat=lat, lng=lng, category=category, radius=radius
    )
    return {"status": "success", "data": data}


@router.get("/issuance/schedules")
async def get_issuance_schedules(
    sigungu_code: str,
    status: str = Query("OPEN", pattern="^(OPEN|CLOSED)$"),
):
    data = await schedule_service.get_schedules(sigungu_code, status=status)
    return {"status": "success", "data": data}


# ---- 관리자 엔드포인트 (admin_id/admin_pw 쿼리 파라미터 필요, BLOG_ADMIN_ID/PW와 동일 계정) ----


@admin_router.get("/locakcur", response_class=HTMLResponse)
async def admin_dashboard_page():
    """브라우저용 관리자 대시보드 - 발행 일정 등록/수정, 미검토 공지 확인,
    동기화 로그 조회. 아이디/비밀번호는 페이지 안 로그인 폼에서 입력받는다
    (이 GET 자체는 인증 없이 정적 HTML만 반환 - 실제 데이터 호출은 모두
    JS에서 쿼리 파라미터로 실어 보낸다)."""
    return HTMLResponse(content=locakcur_admin_page.HTML)


@admin_router.get("/schedules", dependencies=[Depends(_require_admin)])
async def admin_list_schedules(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    publication_status: str | None = Query(None, pattern="^(DRAFT|PUBLISHED|ARCHIVED)$"),
):
    data = await schedule_service.list_all_schedules(
        limit=limit, offset=offset, query=q, publication_status=publication_status
    )
    return {"status": "success", "data": data}


@admin_router.post("/schedules", dependencies=[Depends(_require_admin)])
async def admin_create_schedule(payload: ScheduleInput):
    data = await schedule_service.create_schedule(payload.model_dump(mode="json"))
    return {"status": "success", "data": data}


@admin_router.put("/schedules/{schedule_id}", dependencies=[Depends(_require_admin)])
async def admin_update_schedule(schedule_id: int, payload: ScheduleInput):
    data = await schedule_service.update_schedule(schedule_id, payload.model_dump(mode="json"))
    if data is None:
        raise HTTPException(404, "schedule not found")
    return {"status": "success", "data": data}


@admin_router.get("/crawler-sources", dependencies=[Depends(_require_admin)])
async def admin_crawler_sources():
    """관리자 대시보드 '크롤링 대상' 탭 - 실제로 도는 크롤러 대상 사이트와
    URL, 검토했으나 제외한 후보까지 그대로 노출한다."""
    return {"status": "success", "data": notice_crawler_service.PLATFORM_INFO}


@admin_router.get("/notices/unreviewed", dependencies=[Depends(_require_admin)])
async def admin_unreviewed_notices(limit: int = Query(50, ge=1, le=200)):
    """크롤러가 모은 원문 공지 중 아직 일정으로 안 옮긴 것들 - 운영자가
    이걸 보고 POST /admin/schedules로 구조화해 등록한다."""
    data = await crawled_notice_service.list_unreviewed(limit=limit)
    return {"status": "success", "data": data}


@admin_router.post("/notices/{notice_id}/reviewed", dependencies=[Depends(_require_admin)])
async def admin_mark_notice_reviewed(notice_id: int):
    data = await crawled_notice_service.mark_reviewed(notice_id)
    if data is None:
        raise HTTPException(404, "notice not found")
    return {"status": "success", "data": data}


@admin_router.post("/notices/{notice_id}/publish", dependencies=[Depends(_require_admin)])
async def admin_publish_notice(notice_id: int, payload: ScheduleInput):
    notice = await crawled_notice_service.get_notice(notice_id)
    if notice is None:
        raise HTTPException(404, "notice not found")
    if notice.get("linked_schedule_id"):
        raise HTTPException(409, "notice already linked to a schedule")
    values = payload.model_dump(mode="json")
    values.update({
        "publication_status": "PUBLISHED",
        "source": notice["platform"],
        "source_notice_id": notice["notice_id"],
        "web_url": values.get("web_url") or notice.get("url"),
    })
    schedule = await schedule_service.create_schedule(values)
    await crawled_notice_service.mark_reviewed(notice_id, linked_schedule_id=schedule["id"])
    return {"status": "success", "data": schedule}


@admin_router.get("/sync-log", dependencies=[Depends(_require_admin)])
async def admin_sync_log(limit: int = Query(20, ge=1, le=100)):
    data = await sync_log_service.recent_runs(limit=limit)
    return {"status": "success", "data": data}


@admin_router.post("/sync/run", dependencies=[Depends(_require_admin)])
async def admin_trigger_sync():
    """새벽 3시 cron과 동일한 작업을 수동으로 즉시 실행 (테스트/긴급 재수집용)."""
    result = await schedule_sync_job.run_daily_sync()
    return {"status": "success", "data": result}
