"""App Monetization Dashboard - 집계 계층.

google_ads_daily(광고비) + admob_daily(광고수익) + analytics_daily(사용자/리텐션)를
app / country / campaign / date 기준으로 묶어 dashboard_calc의 계산 함수에 넘긴다.

중요한 근사치 하나: AdMob 수익은 어떤 UAC 캠페인이 그 사용자를 데려왔는지 모른다
(AdMob 자체에는 캠페인 attribution이 없음). 그래서 캠페인별 revenue/ROAS는
해당 app+country+date 내 캠페인의 설치수 비중으로 AdMob 수익을 "비례 배분"한
추정치다. API 응답에 estimated=True로 표시해 실제값과 혼동되지 않게 한다.

두 번째 근사치: 진짜 코호트 기반 D30 LTV(설치일 기준 30일 누적 수익)는 유저 단위
attribution이 없어 AdMob/GA4 표준 리포트로는 계산할 수 없다(BigQuery Export 연동이
있어야 함 - 아직 미구현). 그 대신 build_app_table()은 "최근 30일 AdMob 수익 / 최근
30일 GA4 신규유저수"로 블렌디드 근사치를 만들고 ltv_estimated=True로 표시한다.
IAP 수익/유료전환율은 이 저장소에 연동 자체가 없어(Play Developer API 등) 필드를
아예 만들지 않는다 - BigQuery Export + IAP 연동이 붙으면 그때 추가한다.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from services import dashboard_calc as calc
from services import dashboard_db as db
from services import dashboard_mock as mock
from services.google_ads_service import INSTALL_ONLY_COUNTRY


def _exclude_install_only_rows(rows: list[dict]) -> list[dict]:
    """국가별 집계에서 fetch_campaign_installs_daily()가 남긴 설치수 전용 행을 제외한다."""
    return [r for r in rows if r.get("country") != INSTALL_ONLY_COUNTRY]


def is_mock_mode() -> bool:
    import os
    return os.getenv("USE_MOCK_DATA", "true").strip().lower() in ("1", "true", "yes")


async def fetch_raw(
    start: date,
    end: date,
    app_id: str | None = None,
    country: str | None = None,
    campaign_id: str | None = None,
) -> dict:
    if is_mock_mode():
        return {
            "ads": mock.get_mock_google_ads_daily(start, end, app_id, country, campaign_id),
            "admob": mock.get_mock_admob_daily(start, end, app_id, country),
            "analytics": mock.get_mock_analytics_daily(start, end, app_id, country),
            "apps": mock.get_mock_apps(),
            "countries": mock.get_mock_countries(),
            "campaigns": mock.get_mock_campaigns(app_id),
        }

    filters = {}
    if app_id:
        filters["app_id"] = app_id
    if country:
        filters["country"] = country
    ads_filters = dict(filters)
    if campaign_id:
        ads_filters["campaign_id"] = campaign_id

    return {
        "ads": await db.get_google_ads_daily(start, end, **ads_filters),
        "admob": await db.get_admob_daily(start, end, **filters),
        "analytics": await db.get_analytics_daily(start, end, **filters),
        "apps": await db.get_apps(),
        "countries": await db.get_countries(),
        "campaigns": await db.get_campaigns(app_id),
    }


async def _fetch_trailing_30d_ltv_inputs() -> tuple[dict[str, dict], dict[str, dict]]:
    """"D30 LTV" 라벨이 의미를 유지하도록, 대시보드에서 고른 기간과 무관하게 항상
    오늘 기준 최근 30일 고정 윈도우로 앱별 AdMob 수익 합계 / GA4 신규유저 합계를 가져온다.
    반환: (admob_by_app, analytics_by_app) - 둘 다 sum_by()로 만든 {(app_id,): {..}} 형태."""
    end_d = date.today()
    start_d = end_d - timedelta(days=29)
    admob_rows = await db.get_admob_daily(start_d, end_d)
    analytics_rows = await db.get_analytics_daily(start_d, end_d)
    admob_by_app = sum_by(admob_rows, ("app_id",), ("estimated_earnings",))
    analytics_by_app = sum_by(analytics_rows, ("app_id",), ("new_users",))
    return admob_by_app, analytics_by_app


def _avg_retention_by_app(rows: list[dict]) -> dict[str, dict]:
    """앱별 retention_d1/d7/d30 평균 (해당 기간 raw["analytics"] 행 기준)."""
    sums: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    for r in rows:
        app_id = r.get("app_id")
        if not app_id:
            continue
        bucket = sums[app_id]
        bucket[0] += r.get("retention_d1") or 0
        bucket[1] += r.get("retention_d7") or 0
        bucket[2] += r.get("retention_d30") or 0
        bucket[3] += 1
    return {
        app_id: {
            "retention_d1": round(d1 / n, 4),
            "retention_d7": round(d7 / n, 4),
            "retention_d30": round(d30 / n, 4),
        }
        for app_id, (d1, d7, d30, n) in sums.items()
        if n > 0
    }


def sum_by(rows: list[dict], keys: tuple[str, ...], fields: tuple[str, ...]) -> dict[tuple, dict]:
    out: dict[tuple, dict] = defaultdict(lambda: {f: 0 for f in fields})
    for row in rows:
        k = tuple(row.get(key) for key in keys)
        bucket = out[k]
        for f in fields:
            bucket[f] += row.get(f) or 0
    return out


async def get_thresholds_and_target(settings: dict | None = None) -> tuple[calc.RoasThresholds, float]:
    if settings is None:
        settings = dict(db.DEFAULT_SETTINGS) if not is_mock_mode() else dict(db.DEFAULT_SETTINGS)
    thresholds = calc.RoasThresholds(
        scale_min=settings.get("roas_scale_min", 120.0),
        watch_min=settings.get("roas_watch_min", 80.0),
        optimize_min=settings.get("roas_optimize_min", 50.0),
    )
    target_roas = settings.get("target_roas_pct", 100.0)
    return thresholds, target_roas


def _cohort_ltv_for_app(cohorts: list[dict], app_id: str | None) -> tuple[float, int]:
    relevant = [c for c in cohorts if not app_id or c["app_id"] == app_id]
    revenue = sum(c["d30_revenue"] for c in relevant)
    users = sum(c["users"] for c in relevant)
    return revenue, users


async def build_summary(start: date, end: date, app_id: str | None = None, country: str | None = None) -> dict:
    raw = await fetch_raw(start, end, app_id, country)
    ads_total = sum_by(raw["ads"], (), ("cost", "clicks", "impressions", "installs"))[()]
    admob_total = sum_by(raw["admob"], (), ("estimated_earnings",))[()]
    cohorts = mock.get_mock_cohorts(app_id) if is_mock_mode() else []
    cohort_revenue, cohort_users = _cohort_ltv_for_app(cohorts, app_id)

    row = calc.ProfitabilityRow(
        ad_spend=ads_total["cost"],
        ad_revenue=admob_total["estimated_earnings"],
        installs=ads_total["installs"],
        clicks=ads_total["clicks"],
        impressions=ads_total["impressions"],
        cohort_revenue_d30=cohort_revenue,
        cohort_users_d30=cohort_users,
    ).compute()

    if row.profit >= 0:
        status_message = "현재 광고수익이 광고비를 상회하고 있습니다."
    else:
        status_message = "현재 광고비 대비 광고수익이 부족합니다."

    return {
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "ad_spend": round(row.ad_spend, 2),
        "ad_revenue": round(row.ad_revenue, 2),
        "profit": round(row.profit, 2),
        "roas": round(row.roas, 1),
        "installs": row.installs,
        "cpi": round(row.cpi, 4),
        "d30_ltv": round(row.d30_ltv, 4),
        "status": calc.classify_status(row.roas),
        "status_message": status_message,
        "mock_data": is_mock_mode(),
    }


async def build_app_table(start: date, end: date) -> list[dict]:
    raw = await fetch_raw(start, end)
    ads_by_app = sum_by(raw["ads"], ("app_id",), ("cost", "clicks", "impressions", "installs"))
    admob_by_app = sum_by(raw["admob"], ("app_id",), ("estimated_earnings",))
    analytics_by_app = sum_by(raw["analytics"], ("app_id",), ("new_users", "active_users"))
    retention_by_app = _avg_retention_by_app(raw["analytics"])
    cohorts = mock.get_mock_cohorts() if is_mock_mode() else []

    # 근사 D30 LTV용: 사용자가 고른 기간과 무관하게 항상 최근 30일 고정 데이터.
    ltv_admob_30d, ltv_analytics_30d = (
        (None, None) if is_mock_mode() else await _fetch_trailing_30d_ltv_inputs()
    )

    app_ids = {a["app_id"] for a in raw["apps"]} | {k[0] for k in ads_by_app} | {k[0] for k in admob_by_app}
    app_names = {a["app_id"]: a["app_name"] for a in raw["apps"]}

    result = []
    for app_id in app_ids:
        ads = ads_by_app.get((app_id,), {"cost": 0, "clicks": 0, "impressions": 0, "installs": 0})
        admob = admob_by_app.get((app_id,), {"estimated_earnings": 0})
        cohort_revenue, cohort_users = _cohort_ltv_for_app(cohorts, app_id)
        row = calc.ProfitabilityRow(
            ad_spend=ads["cost"],
            ad_revenue=admob["estimated_earnings"],
            installs=ads["installs"],
            clicks=ads["clicks"],
            impressions=ads["impressions"],
            cohort_revenue_d30=cohort_revenue,
            cohort_users_d30=cohort_users,
        ).compute()

        analytics = analytics_by_app.get((app_id,))
        analytics_available = analytics is not None
        ltv_estimated = False
        if is_mock_mode():
            # 기존 목업 코호트 기반 d30_ltv/ltv_to_cpi를 그대로 사용.
            arpu = row.arpu
            ad_impressions_per_user = calc.safe_div(ads["impressions"], analytics["active_users"]) if analytics else None
            ad_revenue_per_user = arpu if analytics else None
            retention = retention_by_app.get(app_id)
        elif analytics_available:
            retention = retention_by_app.get(app_id)
            arpu = calc.arpu(admob["estimated_earnings"], analytics["active_users"])
            ad_impressions_per_user = calc.safe_div(ads["impressions"], analytics["active_users"])
            ad_revenue_per_user = arpu
            admob_30d = (ltv_admob_30d or {}).get((app_id,), {"estimated_earnings": 0})
            analytics_30d = (ltv_analytics_30d or {}).get((app_id,), {"new_users": 0})
            row.d30_ltv = calc.ltv(admob_30d["estimated_earnings"], analytics_30d["new_users"])
            row.ltv_to_cpi = calc.ltv_to_cpi(row.d30_ltv, row.cpi)
            ltv_estimated = True
        else:
            retention = None
            arpu = None
            ad_impressions_per_user = None
            ad_revenue_per_user = None
            row.d30_ltv = 0.0
            row.ltv_to_cpi = 0.0

        result.append({
            "app_id": app_id,
            "app_name": app_names.get(app_id, app_id),
            "analytics_available": analytics_available,
            "retention_d1": retention["retention_d1"] if retention else None,
            "retention_d7": retention["retention_d7"] if retention else None,
            "retention_d30": retention["retention_d30"] if retention else None,
            "arpu": round(arpu, 4) if arpu is not None else None,
            "ad_impressions_per_user": round(ad_impressions_per_user, 4) if ad_impressions_per_user is not None else None,
            "ad_revenue_per_user": round(ad_revenue_per_user, 4) if ad_revenue_per_user is not None else None,
            "ltv_to_cpi": round(row.ltv_to_cpi, 4) if analytics_available or is_mock_mode() else None,
            "ltv_estimated": ltv_estimated,
            "ad_spend": round(row.ad_spend, 2),
            "ad_revenue": round(row.ad_revenue, 2),
            "profit": round(row.profit, 2),
            "roas": round(row.roas, 1),
            "installs": row.installs,
            "cpi": round(row.cpi, 4),
            "d30_ltv": round(row.d30_ltv, 4),
            "status": row.status,
        })
    result.sort(key=lambda r: r["profit"], reverse=True)
    return result


async def build_country_table(start: date, end: date, app_id: str | None = None) -> list[dict]:
    raw = await fetch_raw(start, end, app_id=app_id)
    ads_by_country = sum_by(_exclude_install_only_rows(raw["ads"]), ("country",), ("cost", "clicks", "impressions", "installs"))
    admob_by_country = sum_by(raw["admob"], ("country",), ("estimated_earnings",))
    country_names = {c["country_code"]: c["country_name"] for c in raw["countries"]}

    country_codes = set(ads_by_country) | set(admob_by_country)
    result = []
    for (country_code,) in country_codes:
        ads = ads_by_country.get((country_code,), {"cost": 0, "clicks": 0, "impressions": 0, "installs": 0})
        admob = admob_by_country.get((country_code,), {"estimated_earnings": 0})
        row = calc.ProfitabilityRow(
            ad_spend=ads["cost"],
            ad_revenue=admob["estimated_earnings"],
            installs=ads["installs"],
            clicks=ads["clicks"],
            impressions=ads["impressions"],
        ).compute()
        result.append({
            "country": country_code,
            "country_name": country_names.get(country_code, country_code),
            "spend": round(row.ad_spend, 2),
            "installs": row.installs,
            "cpi": round(row.cpi, 4),
            "revenue": round(row.ad_revenue, 2),
            "roas": round(row.roas, 1),
            "profit": round(row.profit, 2),
            "status": row.status,
        })
    result.sort(key=lambda r: r["installs"], reverse=True)
    return result


async def build_campaign_table(start: date, end: date, app_id: str | None = None) -> list[dict]:
    """캠페인별 성과. revenue/roas/profit은 설치수 비중 기반 추정치(estimated=True)."""
    raw = await fetch_raw(start, end, app_id=app_id)
    ads_by_campaign = sum_by(raw["ads"], ("campaign_id", "app_id"), ("cost", "clicks", "impressions", "installs", "conversions"))
    admob_by_app = sum_by(raw["admob"], ("app_id",), ("estimated_earnings",))
    ads_by_app_installs = sum_by(raw["ads"], ("app_id",), ("installs",))
    campaign_names = {c["campaign_id"]: c["campaign_name"] for c in raw["campaigns"]}
    campaign_settings = {c["campaign_id"]: c for c in raw["campaigns"]}

    result = []
    for (campaign_id, camp_app_id), ads in ads_by_campaign.items():
        app_installs = ads_by_app_installs.get((camp_app_id,), {"installs": 0})["installs"]
        app_revenue = admob_by_app.get((camp_app_id,), {"estimated_earnings": 0})["estimated_earnings"]
        share = calc.safe_div(ads["installs"], app_installs)
        estimated_revenue = app_revenue * share

        row = calc.ProfitabilityRow(
            ad_spend=ads["cost"],
            ad_revenue=estimated_revenue,
            installs=ads["installs"],
            clicks=ads["clicks"],
            impressions=ads["impressions"],
        ).compute()
        result.append({
            "campaign_id": campaign_id,
            "campaign_name": campaign_names.get(campaign_id, campaign_id),
            "app_id": camp_app_id,
            "spend": round(row.ad_spend, 2),
            "clicks": row.clicks,
            "installs": row.installs,
            "conversions": ads["conversions"],
            "cpi": round(row.cpi, 4),
            "revenue": round(row.ad_revenue, 2),
            "roas": round(row.roas, 1),
            "profit": round(row.profit, 2),
            "status": row.status,
            "revenue_estimated": True,
            "target_cpi": campaign_settings.get(campaign_id, {}).get("target_cpi"),
            "daily_budget": campaign_settings.get(campaign_id, {}).get("daily_budget"),
            "last_target_cpi_changed_at": campaign_settings.get(campaign_id, {}).get("target_cpi_changed_at"),
            "bidding_strategy_goal_type": campaign_settings.get(campaign_id, {}).get("bidding_strategy_goal_type"),
        })
    result.sort(key=lambda r: r["profit"], reverse=True)
    return result


async def build_timeseries(start: date, end: date, app_id: str | None = None, country: str | None = None) -> dict:
    raw = await fetch_raw(start, end, app_id, country)
    ads_by_date = sum_by(raw["ads"], ("date",), ("cost", "installs"))
    admob_by_date = sum_by(raw["admob"], ("date",), ("estimated_earnings",))

    days = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)

    series = []
    cum_spend = 0.0
    cum_revenue = 0.0
    break_even_day = None
    for day in days:
        day_ads = ads_by_date.get((day,), {"cost": 0, "installs": 0})
        spend = day_ads["cost"]
        installs = day_ads["installs"]
        revenue = admob_by_date.get((day,), {"estimated_earnings": 0})["estimated_earnings"]
        cum_spend += spend
        cum_revenue += revenue
        if break_even_day is None and cum_revenue >= cum_spend and cum_spend > 0:
            break_even_day = day
        series.append({
            "date": day,
            "spend": round(spend, 2),
            "revenue": round(revenue, 2),
            "installs": installs,
            "cpi": round(calc.cpi(spend, installs), 4),
            "cumulative_spend": round(cum_spend, 2),
            "cumulative_revenue": round(cum_revenue, 2),
        })

    return {"series": series, "break_even_day": break_even_day}


async def build_recommendations(start: date, end: date) -> list[dict]:
    app_rows = await build_app_table(start, end)
    country_raw = await fetch_raw(start, end)
    ads_by_app_country = sum_by(_exclude_install_only_rows(country_raw["ads"]), ("app_id", "country"), ("cost", "clicks", "impressions", "installs"))
    admob_by_app_country = sum_by(country_raw["admob"], ("app_id", "country"), ("estimated_earnings",))
    app_names = {a["app_id"]: a["app_name"] for a in country_raw["apps"]}
    country_names = {c["country_code"]: c["country_name"] for c in country_raw["countries"]}
    cohorts = mock.get_mock_cohorts() if is_mock_mode() else []

    recs = []
    keys = set(ads_by_app_country) | set(admob_by_app_country)
    for app_id, country_code in keys:
        ads = ads_by_app_country.get((app_id, country_code), {"cost": 0, "clicks": 0, "impressions": 0, "installs": 0})
        admob = admob_by_app_country.get((app_id, country_code), {"estimated_earnings": 0})
        if ads["cost"] < 10:
            continue  # 표본이 너무 작은 조합은 추천에서 제외
        cohort_revenue, cohort_users = _cohort_ltv_for_app(cohorts, app_id)
        row = calc.ProfitabilityRow(
            ad_spend=ads["cost"],
            ad_revenue=admob["estimated_earnings"],
            installs=ads["installs"],
            cohort_revenue_d30=cohort_revenue,
            cohort_users_d30=cohort_users,
        ).compute()
        name = f"{app_names.get(app_id, app_id)} / {country_names.get(country_code, country_code)}"
        rec = calc.diagnose(name=name, roas_pct=row.roas, cpi_value=row.cpi, d30_ltv_value=row.d30_ltv)
        rec["app_id"] = app_id
        rec["country"] = country_code
        recs.append(rec)

    order = {"SCALE": 0, "STOP": 1, "OPTIMIZE": 2, "WATCH": 3}
    recs.sort(key=lambda r: order.get(r["status"], 9))
    return recs
