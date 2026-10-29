from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from . import repository
from .admin_page import HTML
from .pipeline import run_weekly_collection
from .schemas import TrendUpdate

public_router = APIRouter(prefix="/api/kfood-trends", tags=["kfood-trends"])
admin_router = APIRouter(prefix="/admin/kfood-trends", tags=["kfood-trends-admin"])


def require_admin(admin_id: str = "", admin_pw: str = "") -> None:
    expected_id = os.getenv("BLOG_ADMIN_ID", "")
    expected_pw = os.getenv("BLOG_ADMIN_PW", "")
    if not expected_id or not expected_pw or not (
        secrets.compare_digest(admin_id, expected_id) and secrets.compare_digest(admin_pw, expected_pw)
    ):
        raise HTTPException(401, "관리자 인증에 실패했습니다")


@public_router.get("")
async def list_published(period: Literal["weekly", "monthly", "all"] = "weekly"):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7 if period == "weekly" else 31) if period != "all" else None
    return {"status": "success", "data": await repository.public_list(start)}


@public_router.get("/{trend_id}")
async def get_published(trend_id: str):
    row = await repository.public_detail(trend_id)
    if row is None:
        raise HTTPException(404, "trend not found")
    return {"status": "success", "data": row}


@admin_router.get("", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(HTML)


@admin_router.get("/items", dependencies=[Depends(require_admin)])
async def list_admin_items(status: Literal["draft", "published", "rejected"] | None = None):
    return {"status": "success", "data": await repository.admin_list(status)}


@admin_router.put("/{trend_id}", dependencies=[Depends(require_admin)])
async def update_item(trend_id: str, payload: TrendUpdate):
    values = payload.model_dump(exclude_unset=True)
    row = await repository.update(trend_id, values)
    if row is None:
        raise HTTPException(404, "trend not found")
    return {"status": "success", "data": row}


@admin_router.post("/{trend_id}/publish", dependencies=[Depends(require_admin)])
async def publish_item(trend_id: str, allow_missing_image: bool = False):
    current = await repository.admin_detail(trend_id)
    if current is None:
        raise HTTPException(404, "trend not found")
    if not current.get("image_url") and not allow_missing_image:
        raise HTTPException(409, "대표 이미지가 없습니다. 이미지 URL을 입력한 뒤 승인하세요.")
    row = await repository.set_status(trend_id, "published")
    return {"status": "success", "data": row}


@admin_router.post("/{trend_id}/reject", dependencies=[Depends(require_admin)])
async def reject_item(trend_id: str):
    row = await repository.set_status(trend_id, "rejected")
    if row is None:
        raise HTTPException(404, "trend not found")
    return {"status": "success", "data": row}


@admin_router.post("/collect", dependencies=[Depends(require_admin)])
async def collect_now():
    result = await run_weekly_collection(trigger="manual")
    return {"status": "success", "data": result}
