"""App Monetization Dashboard - Mock Data Generator.

Google Ads / AdMob / GA4 인증 전에도 Dashboard UI와 분석 로직(수익성 계산,
Cohort, Recommendation)을 개발/검증할 수 있도록 현실적인 고정(seed) 데이터를 만든다.

- 랜덤이 아니라 "일관된" 데이터: 같은 (app, country, date) 조합은 항상 같은 값을 반환한다.
- 앱마다 성과 프로필(수익성 좋음/보통/나쁨)을 미리 정해 두어, plan.md 예시(Noise=SCALE,
  K-Food/Cleaner=STOP)와 동일한 결과가 나오도록 구성했다.
- 실제 데이터가 연결되면 이 모듈은 더 이상 호출되지 않고 dashboard_db.py 경로로 대체된다.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

APPS = [
    {"app_id": "noise", "app_name": "Noise", "platform": "android", "package_name": "com.hustle.noise", "profile": "high"},
    {"app_id": "keysmash", "app_name": "KeySmash", "platform": "android", "package_name": "com.hustle.keysmash", "profile": "good"},
    {"app_id": "vibrationcheck", "app_name": "VibrationCheck", "platform": "android", "package_name": "com.hustle.vibrationcheck", "profile": "mid"},
    {"app_id": "kfood", "app_name": "K-Food", "platform": "android", "package_name": "com.hustle.kfood", "profile": "low"},
    {"app_id": "cleaner", "app_name": "Cleaner", "platform": "android", "package_name": "com.hustle.cleaner", "profile": "low"},
]

COUNTRIES = [
    {"country_code": "JP", "country_name": "Japan"},
    {"country_code": "US", "country_name": "USA"},
    {"country_code": "KR", "country_name": "Korea"},
    {"country_code": "CA", "country_name": "Canada"},
    {"country_code": "TR", "country_name": "Turkey"},
    {"country_code": "MX", "country_name": "Mexico"},
    {"country_code": "GB", "country_name": "UK"},
    {"country_code": "DE", "country_name": "Germany"},
]

# 프로필별 성과 바이어스 (mock 전용 튜닝 값. 실제 데이터와 무관)
_PROFILE_BIAS = {
    "high": {"roas": 1.42, "cpi": 0.38, "retention": 1.15},
    "good": {"roas": 1.10, "cpi": 0.55, "retention": 1.05},
    "mid": {"roas": 0.85, "cpi": 0.70, "retention": 0.95},
    "low": {"roas": 0.22, "cpi": 0.28, "retention": 0.55},
}

AD_UNIT_TYPES = ["banner", "interstitial", "native"]


def _rng(*parts: str) -> random.Random:
    """(app, country, date, ...) 조합마다 안정적인 seed를 만든다."""
    key = "|".join(str(p) for p in parts)
    seed = int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


def get_mock_apps() -> list[dict]:
    return [{k: v for k, v in a.items() if k != "profile"} for a in APPS]


def _app_by_id(app_id: str) -> dict | None:
    return next((a for a in APPS if a["app_id"] == app_id), None)


def get_mock_countries() -> list[dict]:
    return list(COUNTRIES)


def get_mock_campaigns(app_id: str | None = None) -> list[dict]:
    campaigns = []
    for app in APPS:
        if app_id and app["app_id"] != app_id:
            continue
        for suffix, label in (("uac_install", "UAC Installs"), ("uac_troas", "UAC tROAS")):
            campaigns.append({
                "campaign_id": f"{app['app_id']}_{suffix}",
                "campaign_name": f"{app['app_name']} {label}",
                "app_id": app["app_id"],
                "status": "active",
                "target_cpi": round(_PROFILE_BIAS[app["profile"]]["cpi"] * 1.15, 4) if suffix == "uac_install" else None,
                "daily_budget": 25.0,
                "target_cpi_changed_at": None,
                "bidding_strategy_goal_type": "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST" if suffix == "uac_install" else "OPTIMIZE_RETURN_ON_ADVERTISING_SPEND",
            })
    return campaigns


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def get_mock_google_ads_daily(
    start: date,
    end: date,
    app_id: str | None = None,
    country: str | None = None,
    campaign_id: str | None = None,
) -> list[dict]:
    rows = []
    apps = [a for a in APPS if not app_id or a["app_id"] == app_id]
    countries = [c for c in COUNTRIES if not country or c["country_code"] == country]
    campaigns = get_mock_campaigns()
    if campaign_id:
        campaigns = [c for c in campaigns if c["campaign_id"] == campaign_id]

    for app in apps:
        bias = _PROFILE_BIAS[app["profile"]]
        app_campaigns = [c for c in campaigns if c["app_id"] == app["app_id"]]
        for camp in app_campaigns:
            for country_row in countries:
                for d in _date_range(start, end):
                    rng = _rng(app["app_id"], camp["campaign_id"], country_row["country_code"], d.isoformat())
                    impressions = rng.randint(2000, 20000)
                    ctr_val = rng.uniform(0.015, 0.06)
                    clicks = max(1, int(impressions * ctr_val))
                    cpc_val = round(bias["cpi"] * rng.uniform(0.4, 0.7), 4)
                    cost = round(clicks * cpc_val, 2)
                    install_rate = rng.uniform(0.18, 0.35)
                    installs = max(0, int(clicks * install_rate))
                    conversions = installs
                    rows.append({
                        "date": d.isoformat(),
                        "account_id": "mock-account",
                        "campaign_id": camp["campaign_id"],
                        "campaign_name": camp["campaign_name"],
                        "campaign_status": camp["status"],
                        "app_id": app["app_id"],
                        "country": country_row["country_code"],
                        "currency": "USD",
                        "impressions": impressions,
                        "clicks": clicks,
                        "cost": cost,
                        "conversions": conversions,
                        "installs": installs,
                    })
    return rows


def get_mock_admob_daily(
    start: date,
    end: date,
    app_id: str | None = None,
    country: str | None = None,
    ad_unit_id: str | None = None,
) -> list[dict]:
    rows = []
    apps = [a for a in APPS if not app_id or a["app_id"] == app_id]
    countries = [c for c in COUNTRIES if not country or c["country_code"] == country]

    for app in apps:
        bias = _PROFILE_BIAS[app["profile"]]
        for ad_type in AD_UNIT_TYPES:
            unit_id = f"{app['app_id']}_{ad_type}"
            if ad_unit_id and unit_id != ad_unit_id:
                continue
            for country_row in countries:
                for d in _date_range(start, end):
                    rng = _rng(app["app_id"], unit_id, country_row["country_code"], d.isoformat())
                    ad_requests = rng.randint(5000, 40000)
                    match_rate_val = rng.uniform(0.75, 0.97)
                    matched_requests = int(ad_requests * match_rate_val)
                    fill_ctr = rng.uniform(0.01, 0.05) if ad_type != "native" else rng.uniform(0.03, 0.09)
                    impressions = int(matched_requests * rng.uniform(0.9, 1.0))
                    clicks = max(0, int(impressions * fill_ctr))
                    base_ecpm = {"banner": 2.5, "interstitial": 8.0, "native": 6.0}[ad_type]
                    ecpm_val = round(base_ecpm * bias["roas"] * rng.uniform(0.7, 1.3), 4)
                    earnings = round((impressions / 1000) * ecpm_val, 2)
                    rows.append({
                        "date": d.isoformat(),
                        "app_id": app["app_id"],
                        "app_name": app["app_name"],
                        "ad_unit_id": unit_id,
                        "ad_unit_type": ad_type,
                        "country": country_row["country_code"],
                        "currency": "USD",
                        "ad_requests": ad_requests,
                        "matched_requests": matched_requests,
                        "impressions": impressions,
                        "clicks": clicks,
                        "estimated_earnings": earnings,
                        "ecpm": ecpm_val,
                        "match_rate": round(match_rate_val, 4),
                    })
    return rows


def get_mock_analytics_daily(
    start: date,
    end: date,
    app_id: str | None = None,
    country: str | None = None,
) -> list[dict]:
    rows = []
    apps = [a for a in APPS if not app_id or a["app_id"] == app_id]
    countries = [c for c in COUNTRIES if not country or c["country_code"] == country]

    for app in apps:
        bias = _PROFILE_BIAS[app["profile"]]
        for country_row in countries:
            for d in _date_range(start, end):
                rng = _rng("ga4", app["app_id"], country_row["country_code"], d.isoformat())
                new_users = rng.randint(50, 900)
                active_users = int(new_users * rng.uniform(2.0, 4.5))
                sessions = int(active_users * rng.uniform(1.5, 3.0))
                retention_d1 = round(min(0.95, rng.uniform(0.25, 0.45) * bias["retention"]), 4)
                retention_d7 = round(min(0.7, retention_d1 * rng.uniform(0.45, 0.6)), 4)
                retention_d30 = round(min(0.4, retention_d7 * rng.uniform(0.35, 0.55)), 4)
                ad_revenue_per_user = 0.015 * bias["roas"] * rng.uniform(0.8, 1.2)
                ad_revenue = round(active_users * ad_revenue_per_user, 2)
                rows.append({
                    "date": d.isoformat(),
                    "app_id": app["app_id"],
                    "country": country_row["country_code"],
                    "new_users": new_users,
                    "active_users": active_users,
                    "sessions": sessions,
                    "retention_d1": retention_d1,
                    "retention_d7": retention_d7,
                    "retention_d30": retention_d30,
                    "ad_revenue": ad_revenue,
                })
    return rows


def get_mock_cohorts(app_id: str | None = None, days: int = 30) -> list[dict]:
    """설치일 기준 Cohort: 최근 `days`일 각각의 설치 cohort에 대한 D1/D7/D30 지표."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    cohorts = []
    apps = [a for a in APPS if not app_id or a["app_id"] == app_id]

    for app in apps:
        bias = _PROFILE_BIAS[app["profile"]]
        for d in _date_range(start, end):
            rng = _rng("cohort", app["app_id"], d.isoformat())
            users = rng.randint(200, 3000)
            arpu_d1 = 0.03 * bias["roas"] * rng.uniform(0.8, 1.2)
            arpu_d7 = arpu_d1 * rng.uniform(2.2, 3.0)
            arpu_d30 = arpu_d7 * rng.uniform(1.8, 2.6)
            retention_d1 = round(min(0.95, rng.uniform(0.25, 0.45) * bias["retention"]), 4)
            retention_d7 = round(min(0.7, retention_d1 * rng.uniform(0.45, 0.6)), 4)
            retention_d30 = round(min(0.4, retention_d7 * rng.uniform(0.35, 0.55)), 4)
            d1_revenue = round(users * arpu_d1, 2)
            d7_revenue = round(users * arpu_d7, 2)
            d30_revenue = round(users * arpu_d30, 2)
            cohorts.append({
                "app_id": app["app_id"],
                "install_date": d.isoformat(),
                "users": users,
                "d1_revenue": d1_revenue,
                "d7_revenue": d7_revenue,
                "d30_revenue": d30_revenue,
                "retention_d1": retention_d1,
                "retention_d7": retention_d7,
                "retention_d30": retention_d30,
                "d1_ltv": round(d1_revenue / users, 4) if users else 0,
                "d7_ltv": round(d7_revenue / users, 4) if users else 0,
                "d30_ltv": round(d30_revenue / users, 4) if users else 0,
            })
    return cohorts
