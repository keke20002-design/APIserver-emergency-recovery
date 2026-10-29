"""App Monetization Dashboard - API 엔드포인트.

main.py의 기존 스타일(개별 @app.get/@app.post)과 달리 이 기능만 APIRouter로 묶었다.
main.py가 이미 매우 크기 때문에(기존 라우트 수백 개), 새 기능 전체를 별도 파일에
격리하고 main.py에는 `include_router` 한 줄만 추가하는 편이 기존 코드에 대한
영향을 최소화한다.
"""
from __future__ import annotations

import logging
import asyncio
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from services import dashboard_aggregate as agg
from services import dashboard_db as db
from services import dashboard_mock as mock
from services import google_ads_service
from services import admob_service
from services import ga4_service
from services import fx_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["monetization-dashboard"])

_campaign_status_lock = asyncio.Lock()
_campaign_status_cache = None
_campaign_status_cached_at = 0.0


@router.get("/google-ads/campaign-status")
async def google_ads_campaign_status():
    global _campaign_status_cache, _campaign_status_cached_at
    async with _campaign_status_lock:
        if _campaign_status_cache is not None and time.monotonic() - _campaign_status_cached_at < 60:
            return _campaign_status_cache
        customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
        if not customer_id:
            raise HTTPException(503, "Google Ads account is not configured")
        try:
            rows = await asyncio.to_thread(google_ads_service.fetch_campaign_statuses, customer_id)
        except Exception:
            logger.exception("Google Ads campaign status lookup failed")
            raise HTTPException(502, "Google Ads 상태 조회 실패")
        _campaign_status_cache = {"campaigns": rows, "checked_at": datetime.now(ZoneInfo("UTC")).isoformat()}
        _campaign_status_cached_at = time.monotonic()
        return _campaign_status_cache


def _resolve_range(start: str | None, end: str | None) -> tuple[date, date]:
    if end:
        end_d = date.fromisoformat(end)
    else:
        end_d = date.today()
    if start:
        start_d = date.fromisoformat(start)
    else:
        start_d = end_d - timedelta(days=29)
    if start_d > end_d:
        raise HTTPException(400, "start must be before end")
    return start_d, end_d


# ---------- Meta ----------

@router.get("/apps")
async def api_list_apps():
    if agg.is_mock_mode():
        return mock.get_mock_apps()
    return await db.get_apps()


@router.get("/fx")
async def api_fx(base: str = "USD"):
    """실시간 환율. 대시보드 금액(원본 USD)을 선택한 통화로 변환할 때 사용."""
    try:
        result = fx_service.get_rates(base)
    except Exception as e:
        raise HTTPException(502, f"환율 조회 실패: {e}")
    return {"base": base, **result}


@router.get("/countries")
async def api_list_countries():
    if agg.is_mock_mode():
        return mock.get_mock_countries()
    return await db.get_countries()


# ---------- App Mapping (Settings) ----------
# USE_MOCK_DATA 값과 무관하게 항상 실제 Supabase를 대상으로 한다 - Google Ads/AdMob 캠페인이
# 실제 앱(app_id)에 매핑되기 전까지는 이 화면에서 관리자가 직접 채운다.

@router.get("/settings/apps")
async def settings_list_apps():
    return await db.get_apps()


@router.post("/settings/apps")
async def settings_create_app(payload: dict):
    if not payload.get("app_id") or not payload.get("app_name"):
        raise HTTPException(400, "app_id, app_name은 필수입니다.")
    allowed = {"app_id", "app_name", "platform", "package_name", "active"}
    data = {k: v for k, v in payload.items() if k in allowed}
    try:
        return await db.create_app(data)
    except Exception as e:
        raise HTTPException(400, f"앱 생성 실패: {e}")


@router.put("/settings/apps/{app_id}")
async def settings_update_app(app_id: str, payload: dict):
    allowed = {"app_name", "platform", "package_name", "active"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    if not patch:
        raise HTTPException(400, "no valid fields")
    try:
        result = await db.update_app(app_id, patch)
    except Exception as e:
        raise HTTPException(400, f"업데이트 실패: {e}")
    if not result:
        raise HTTPException(404, f"app_id={app_id} not found")
    return result


@router.get("/settings/campaigns")
async def settings_list_campaigns():
    return await db.get_campaigns()


@router.put("/settings/campaigns/{campaign_id}")
async def settings_update_campaign(campaign_id: str, payload: dict):
    allowed = {"app_id", "active"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    if not patch:
        raise HTTPException(400, "no valid fields")
    try:
        result = await db.update_campaign(campaign_id, patch)
    except Exception as e:
        raise HTTPException(400, f"업데이트 실패: {e}")
    if not result:
        raise HTTPException(404, f"campaign_id={campaign_id} not found")
    return result


@router.get("/campaigns")
async def api_list_campaigns(app_id: str | None = None):
    if agg.is_mock_mode():
        return mock.get_mock_campaigns(app_id)
    return await db.get_campaigns(app_id)


# ---------- Dashboard (Overview) ----------

@router.get("/dashboard/summary")
async def dashboard_summary(
    start: str | None = None,
    end: str | None = None,
    app_id: str | None = None,
    country: str | None = None,
):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_summary(start_d, end_d, app_id, country)


@router.get("/dashboard/apps")
async def dashboard_apps(start: str | None = None, end: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_app_table(start_d, end_d)


@router.get("/dashboard/countries")
async def dashboard_countries(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_country_table(start_d, end_d, app_id)


@router.get("/dashboard/campaigns")
async def dashboard_campaigns(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_campaign_table(start_d, end_d, app_id)


# ---------- Google Ads ----------

@router.get("/google-ads/summary")
async def google_ads_summary(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    raw = await agg.fetch_raw(start_d, end_d, app_id=app_id)
    from services.dashboard_calc import ctr as ctr_fn, cpc as cpc_fn, cpi as cpi_fn, safe_div

    totals = agg.sum_by(raw["ads"], (), ("cost", "clicks", "impressions", "installs"))[()]
    return {
        "spend": round(totals["cost"], 2),
        "clicks": totals["clicks"],
        "impressions": totals["impressions"],
        "installs": totals["installs"],
        "ctr": round(ctr_fn(totals["clicks"], totals["impressions"]), 4),
        "cpc": round(cpc_fn(totals["cost"], totals["clicks"]), 4),
        "cpi": round(cpi_fn(totals["cost"], totals["installs"]), 4),
    }


@router.get("/google-ads/campaigns")
async def google_ads_campaigns(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_campaign_table(start_d, end_d, app_id)


# ---------- AdMob ----------

@router.get("/admob/summary")
async def admob_summary(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    raw = await agg.fetch_raw(start_d, end_d, app_id=app_id)
    from services.dashboard_calc import ecpm as ecpm_fn, match_rate as match_rate_fn, ctr as ctr_fn

    totals = agg.sum_by(
        raw["admob"], (), ("ad_requests", "matched_requests", "impressions", "clicks", "estimated_earnings")
    )[()]
    return {
        "revenue": round(totals["estimated_earnings"], 2),
        "impressions": totals["impressions"],
        "ad_requests": totals["ad_requests"],
        "matched_requests": totals["matched_requests"],
        "clicks": totals["clicks"],
        "ecpm": round(ecpm_fn(totals["estimated_earnings"], totals["impressions"]), 4),
        "match_rate": round(match_rate_fn(totals["matched_requests"], totals["ad_requests"]), 4),
        "ctr": round(ctr_fn(totals["clicks"], totals["impressions"]), 4),
    }


@router.get("/admob/apps")
async def admob_apps(start: str | None = None, end: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    raw = await agg.fetch_raw(start_d, end_d)
    from services.dashboard_calc import ecpm as ecpm_fn

    by_app = agg.sum_by(raw["admob"], ("app_id",), ("impressions", "estimated_earnings"))
    app_names = {a["app_id"]: a["app_name"] for a in raw["apps"]}
    rows = []
    for (app_id,), totals in by_app.items():
        rows.append({
            "app_id": app_id,
            "app_name": app_names.get(app_id, app_id),
            "revenue": round(totals["estimated_earnings"], 2),
            "impressions": totals["impressions"],
            "ecpm": round(ecpm_fn(totals["estimated_earnings"], totals["impressions"]), 4),
        })
    rows.sort(key=lambda r: r["revenue"], reverse=True)
    return rows


@router.get("/admob/ad-units")
async def admob_ad_units(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    raw = await agg.fetch_raw(start_d, end_d, app_id=app_id)
    from services.dashboard_calc import ecpm as ecpm_fn, match_rate as match_rate_fn

    by_unit = agg.sum_by(
        raw["admob"], ("ad_unit_id",), ("ad_requests", "matched_requests", "impressions", "estimated_earnings")
    )
    rows = []
    for (ad_unit_id,), totals in by_unit.items():
        rows.append({
            "ad_unit_id": ad_unit_id,
            "revenue": round(totals["estimated_earnings"], 2),
            "impressions": totals["impressions"],
            "ecpm": round(ecpm_fn(totals["estimated_earnings"], totals["impressions"]), 4),
            "match_rate": round(match_rate_fn(totals["matched_requests"], totals["ad_requests"]), 4),
        })
    rows.sort(key=lambda r: r["revenue"], reverse=True)
    return rows


# ---------- Analytics (GA4 / Firebase) ----------

@router.get("/analytics/retention")
async def analytics_retention(start: str | None = None, end: str | None = None, app_id: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    raw = await agg.fetch_raw(start_d, end_d, app_id=app_id)
    rows = raw["analytics"]
    if not rows:
        return {"available": False, "reason": "Firebase/GA4 데이터가 아직 연결되지 않았습니다.", "series": []}

    by_date = agg.sum_by(rows, ("date",), ("new_users", "active_users", "sessions"))
    weighted = {}
    for row in rows:
        d = row["date"]
        weighted.setdefault(d, {"r1": [], "r7": [], "r30": []})
        weighted[d]["r1"].append(row["retention_d1"])
        weighted[d]["r7"].append(row["retention_d7"])
        weighted[d]["r30"].append(row["retention_d30"])

    series = []
    for (d,), totals in sorted(by_date.items()):
        avg = weighted.get(d, {"r1": [0], "r7": [0], "r30": [0]})
        series.append({
            "date": d,
            "new_users": totals["new_users"],
            "active_users": totals["active_users"],
            "sessions": totals["sessions"],
            "retention_d1": round(sum(avg["r1"]) / len(avg["r1"]), 4),
            "retention_d7": round(sum(avg["r7"]) / len(avg["r7"]), 4),
            "retention_d30": round(sum(avg["r30"]) / len(avg["r30"]), 4),
        })
    return {"available": True, "series": series}


@router.get("/analytics/cohorts")
async def analytics_cohorts(app_id: str | None = None, days: int = Query(30, ge=1, le=90)):
    if not agg.is_mock_mode():
        return {
            "available": False,
            "reason": (
                "설치일 기준 사용자 단위 Cohort 분석은 GA4 표준 리포트로는 제공되지 않습니다. "
                "BigQuery Export 연동이 필요합니다 (Phase 5 확장 예정)."
            ),
            "cohorts": [],
        }
    return {"available": True, "cohorts": mock.get_mock_cohorts(app_id, days)}


# ---------- Profitability ----------

@router.get("/profitability")
async def profitability(
    start: str | None = None,
    end: str | None = None,
    app_id: str | None = None,
    country: str | None = None,
):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_timeseries(start_d, end_d, app_id, country)


@router.get("/profitability/break-even-cpi")
async def break_even_cpi(app_id: str | None = None, target_roas_pct: float | None = None):
    from services.dashboard_calc import break_even_cpi as calc_break_even

    settings = await db.get_settings() if not agg.is_mock_mode() else dict(db.DEFAULT_SETTINGS)
    target = target_roas_pct if target_roas_pct is not None else settings.get("target_roas_pct", 100.0)

    cohorts = mock.get_mock_cohorts(app_id) if agg.is_mock_mode() else []
    revenue = sum(c["d30_revenue"] for c in cohorts)
    users = sum(c["users"] for c in cohorts)
    d30_ltv = round(revenue / users, 4) if users else 0.0
    return {
        "app_id": app_id,
        "d30_ltv": d30_ltv,
        "target_roas_pct": target,
        "break_even_cpi": round(calc_break_even(d30_ltv, target), 4),
    }


# ---------- Recommendations ----------

@router.get("/recommendations")
async def recommendations(start: str | None = None, end: str | None = None):
    start_d, end_d = _resolve_range(start, end)
    return await agg.build_recommendations(start_d, end_d)


# ---------- Settings ----------

@router.get("/settings")
async def get_settings():
    settings = await db.get_settings() if not agg.is_mock_mode() else dict(db.DEFAULT_SETTINGS)
    connections = {
        "google_ads": "not_configured" if _env_missing("GOOGLE_ADS_DEVELOPER_TOKEN") else "connected",
        "admob": "not_configured" if _env_missing("ADMOB_PUBLISHER_ID") else "connected",
        "firebase": "not_configured" if _env_missing("GA4_PROPERTY_ID") else "connected",
    }
    return {**settings, "connections": connections, "mock_data": agg.is_mock_mode()}


@router.put("/settings")
async def put_settings(patch: dict):
    if agg.is_mock_mode():
        raise HTTPException(400, "USE_MOCK_DATA=true 상태에서는 설정을 저장할 수 없습니다 (Supabase 미사용).")
    allowed = {"base_currency", "timezone", "target_roas_pct", "roas_scale_min", "roas_watch_min", "roas_optimize_min"}
    safe_patch = {k: v for k, v in patch.items() if k in allowed}
    if not safe_patch:
        raise HTTPException(400, "no valid fields")
    return await db.update_settings(safe_patch)


def _env_missing(name: str) -> bool:
    import os
    return not os.getenv(name)


# ---------- Sync ----------

@router.get("/sync/status")
async def sync_status():
    if agg.is_mock_mode():
        return [
            {"source": "google_ads", "status": "mock", "last_synced_at": None},
            {"source": "admob", "status": "mock", "last_synced_at": None},
            {"source": "analytics", "status": "mock", "last_synced_at": None},
        ]
    return await db.get_sync_status()


@router.post("/sync/google-ads")
async def sync_google_ads(days: int = Query(7, ge=1, le=90)):
    return await _sync_google_ads(days)


async def _sync_google_ads(days: int = 7) -> dict:
    """최근 `days`일치 Google Ads 성과를 다시 가져와 upsert.

    최근 며칠은 데이터가 늦게 확정되는 경우가 있어(plan.md #7) 매번 최근 N일을 다시 덮어쓴다.
    라우트 핸들러(days: int = Query(...))를 sync_all()에서 직접 파이썬 함수로 호출하면
    Query 객체가 그대로 남아 timedelta 연산이 깨지므로, 실제 로직은 이 plain 함수에 둔다.
    """
    import os

    customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
    if not customer_id:
        return _not_connected_response("google_ads", "GOOGLE_ADS_DEVELOPER_TOKEN")

    end_d = date.today()
    start_d = end_d - timedelta(days=days - 1)

    try:
        rows = google_ads_service.fetch_campaign_country_daily(customer_id, start_d, end_d)
        rows += google_ads_service.fetch_campaign_installs_daily(customer_id, start_d, end_d)
        campaign_settings = google_ads_service.fetch_campaign_settings(customer_id)
    except google_ads_service.GoogleAdsNotConfigured as e:
        result = {"source": "google_ads", "status": "error", "message": str(e)}
        await db.record_sync_status("google_ads", "error", str(e))
        return result
    except google_ads_service.GoogleAdsAccessDenied as e:
        result = {"source": "google_ads", "status": "error", "message": str(e)}
        await db.record_sync_status("google_ads", "error", str(e))
        return result
    except RuntimeError as e:
        result = {"source": "google_ads", "status": "error", "message": str(e)}
        await db.record_sync_status("google_ads", "error", str(e))
        return result
    except Exception as e:
        result = {"source": "google_ads", "status": "error", "message": f"Google Ads API 오류: {e}"}
        await db.record_sync_status("google_ads", "error", str(e))
        return result

    campaigns_catalog = google_ads_service.extract_campaign_catalog(rows)
    settings_by_campaign = {row["campaign_id"]: row for row in campaign_settings}
    campaigns_catalog = [
        {**catalog, **settings_by_campaign.get(catalog["campaign_id"], {})}
        for catalog in campaigns_catalog
    ]
    known_ids = {row["campaign_id"] for row in campaigns_catalog}
    campaigns_catalog.extend(row for row in campaign_settings if row["campaign_id"] not in known_ids)
    await db.upsert_campaigns_catalog(campaigns_catalog)

    # campaigns 테이블에 관리자가 지정해둔 app_id를 각 행에 반영한다.
    # (upsert_campaigns_catalog는 새로 발견된 캠페인만 app_id=NULL로 넣고 기존 매핑은
    # 건드리지 않으므로, 여기서 최신 매핑을 다시 읽어와 rows에 덮어써야 재동기화 때마다
    # Settings > App Mapping에서 지정한 값이 반영된다.)
    existing_campaigns = await db.get_campaigns()
    app_id_by_campaign = {c["campaign_id"]: c["app_id"] for c in existing_campaigns if c.get("app_id")}
    for row in rows:
        mapped_app_id = app_id_by_campaign.get(row["campaign_id"])
        if mapped_app_id:
            row["app_id"] = mapped_app_id

    saved = await db.upsert_google_ads_daily(rows)

    message = f"{start_d.isoformat()} ~ {end_d.isoformat()}: {len(rows)}건 조회, {saved}건 저장, 캠페인 {len(campaigns_catalog)}개 확인"
    await db.record_sync_status("google_ads", "success", message)
    return {
        "source": "google_ads",
        "status": "success",
        "message": message,
        "rows_fetched": len(rows),
        "rows_saved": saved,
        "campaigns_seen": len(campaigns_catalog),
    }


@router.post("/sync/admob")
async def sync_admob(days: int = Query(7, ge=1, le=90)):
    return await _sync_admob(days)


async def _sync_admob(days: int = 7) -> dict:
    """최근 `days`일치 AdMob 수익 리포트를 다시 가져와 upsert."""
    import os

    publisher_id = os.getenv("ADMOB_PUBLISHER_ID")
    if not publisher_id:
        return _not_connected_response("admob", "ADMOB_PUBLISHER_ID")

    end_d = date.today()
    start_d = end_d - timedelta(days=days - 1)

    try:
        rows = admob_service.fetch_network_report(publisher_id, start_d, end_d)
    except admob_service.AdMobNotConfigured as e:
        result = {"source": "admob", "status": "error", "message": str(e)}
        await db.record_sync_status("admob", "error", str(e))
        return result
    except RuntimeError as e:
        result = {"source": "admob", "status": "error", "message": str(e)}
        await db.record_sync_status("admob", "error", str(e))
        return result
    except Exception as e:
        result = {"source": "admob", "status": "error", "message": f"AdMob API 오류: {e}"}
        await db.record_sync_status("admob", "error", str(e))
        return result

    app_catalog = admob_service.extract_app_catalog(rows)
    await db.upsert_apps_catalog(app_catalog)
    saved = await db.upsert_admob_daily(rows)

    message = f"{start_d.isoformat()} ~ {end_d.isoformat()}: {len(rows)}건 조회, {saved}건 저장, 앱 {len(app_catalog)}개 확인"
    await db.record_sync_status("admob", "success", message)
    return {
        "source": "admob",
        "status": "success",
        "message": message,
        "rows_fetched": len(rows),
        "rows_saved": saved,
        "apps_seen": len(app_catalog),
    }


@router.post("/sync/analytics")
async def sync_analytics(days: int = Query(7, ge=1, le=90)):
    return await _sync_analytics(days)


def _ga4_properties() -> list[dict]:
    """GA4_PROPERTIES(JSON 리스트, [{"property_id":..,"app_id":..,"app_name":..,"stream_id"(선택):..}, ...])가 있으면
    그걸 쓰고, 없으면 기존 단일 변수(GA4_PROPERTY_ID/GA4_APP_ID/GA4_APP_NAME)로 폴백한다.
    앱마다 GA4 연동 여부가 달라서(일부 앱만 Firebase Analytics가 붙어있음) 여러 개를 순회할 수 있게 함."""
    import json
    import os

    raw = os.getenv("GA4_PROPERTIES")
    if raw:
        try:
            return json.loads(raw)
        except ValueError as e:
            logger.warning("GA4_PROPERTIES JSON 파싱 실패, 무시합니다: %s", e)

    property_id = os.getenv("GA4_PROPERTY_ID")
    app_id = os.getenv("GA4_APP_ID")
    if property_id and app_id:
        return [{"property_id": property_id, "app_id": app_id, "app_name": os.getenv("GA4_APP_NAME", app_id)}]
    return []


async def _sync_analytics(days: int = 7) -> dict:
    """최근 `days`일치 GA4 사용자/리텐션/광고수익 데이터를 (설정된 프로퍼티마다) 다시 가져와 upsert."""
    properties = _ga4_properties()
    if not properties:
        return _not_connected_response("analytics", "GA4_PROPERTY_ID")

    end_d = date.today()
    start_d = end_d - timedelta(days=days - 1)

    total_fetched = 0
    total_saved = 0
    per_app_messages = []
    for prop in properties:
        property_id = prop["property_id"]
        app_id = prop["app_id"]
        app_name = prop.get("app_name", app_id)
        try:
            rows = ga4_service.fetch_analytics_daily(property_id, start_d, end_d, prop.get("stream_id"))
        except ga4_service.GA4NotConfigured as e:
            per_app_messages.append(f"{app_name}: 설정 오류({e})")
            continue
        except Exception as e:
            per_app_messages.append(f"{app_name}: GA4 API 오류({e})")
            continue

        for row in rows:
            row["app_id"] = app_id
        await db.upsert_apps_catalog([{"app_id": app_id, "app_name": app_name}])
        saved = await db.upsert_analytics_daily(rows)
        total_fetched += len(rows)
        total_saved += saved
        per_app_messages.append(f"{app_name}: {len(rows)}건 조회, {saved}건 저장")

    message = f"{start_d.isoformat()} ~ {end_d.isoformat()}: " + " / ".join(per_app_messages)
    status = "success" if total_saved > 0 or not any("오류" in m for m in per_app_messages) else "error"
    await db.record_sync_status("analytics", status, message)
    return {
        "source": "analytics",
        "status": status,
        "message": message,
        "rows_fetched": total_fetched,
        "rows_saved": total_saved,
    }


@router.post("/sync/all")
async def sync_all():
    return {
        "google_ads": await _sync_google_ads(),
        "admob": await _sync_admob(),
        "analytics": await _sync_analytics(),
    }


_KST = ZoneInfo("Asia/Seoul")


async def dashboard_sync_already_ran_today() -> bool:
    """세 소스(google_ads/admob/analytics)가 모두 오늘(KST) 동기화된 적이 있으면 True.

    하나라도 기록이 없거나 마지막 동기화가 오늘이 아니면 False — 서버 기동 시
    catch-up 동기화를 돌려야 한다는 뜻."""
    if agg.is_mock_mode():
        return True
    statuses = await db.get_sync_status()
    if not statuses:
        return False
    today_kst = datetime.now(_KST).date()
    synced_sources = set()
    for s in statuses:
        ts = s.get("last_synced_at")
        if not ts:
            continue
        synced_at = datetime.fromisoformat(ts).astimezone(_KST)
        if synced_at.date() == today_kst:
            synced_sources.add(s.get("source"))
    return {"google_ads", "admob", "analytics"} <= synced_sources


async def run_dashboard_sync_job() -> None:
    """일일 자동 동기화 — main.py 스케줄러(매일 정해진 시각)와 서버 기동 시 catch-up
    (오늘 아직 안 돌았을 때) 양쪽에서 호출된다."""
    logger.info("Dashboard 자동 동기화 시작")
    result = await sync_all()
    logger.info("Dashboard 자동 동기화 완료: %s", result)


def _not_connected_response(source: str, required_env: str) -> dict:
    if _env_missing(required_env):
        return {
            "source": source,
            "status": "error",
            "message": f"{required_env} 환경변수가 설정되지 않았습니다. Settings에서 Google Cloud 연동을 먼저 완료하세요.",
        }
    return {
        "source": source,
        "status": "error",
        "message": f"{source} sync는 아직 구현되지 않았습니다 (Phase 3~5 예정).",
    }
