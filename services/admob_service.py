"""AdMob API 연동 - Network Report(광고 수익 리포트) 수집.

인증: Google Ads와 동일한 OAuth 자격증명(GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN)을 재사용한다.
필요한 scope(admob.readonly, admob.report)는 generate_refresh_token.py에서 이미 요청해뒀다.

AdMob은 어떤 앱인지(app_id, app_name)를 리포트 자체에서 알려주므로 - Google Ads 캠페인과
달리 - apps 테이블을 이 리포트만으로 자동 채울 수 있다. 다만 정확한 패키지명은 안 주기
때문에 package_name은 채우지 않는다(Settings > App Mapping에서 관리자가 보완).
"""
from __future__ import annotations

import logging
import os
from datetime import date
from functools import lru_cache

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

API_NAME = "admob"
API_VERSION = "v1"

SCOPES = [
    "https://www.googleapis.com/auth/admob.readonly",
    "https://www.googleapis.com/auth/admob.report",
]


class AdMobNotConfigured(Exception):
    """필수 환경변수가 비어있음."""


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AdMobNotConfigured(f"{name} 환경변수가 설정되지 않았습니다.")
    return value


@lru_cache(maxsize=1)
def _get_service():
    creds = Credentials(
        token=None,
        refresh_token=_required_env("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_required_env("GOOGLE_CLIENT_ID"),
        client_secret=_required_env("GOOGLE_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    return build(API_NAME, API_VERSION, credentials=creds, cache_discovery=False)


def _is_transient_transport_error(exc: Exception) -> bool:
    """Whether a failed cached connection should be discarded and retried."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    message = str(exc).lower()
    return any(token in message for token in (
        "winerror 10053", "winerror 10054", "connection reset", "broken pipe"
    ))


def _date_dict(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day}


def _metric_value(mv: dict) -> float:
    if "microsValue" in mv:
        return int(mv["microsValue"]) / 1_000_000
    if "integerValue" in mv:
        return int(mv["integerValue"])
    if "doubleValue" in mv:
        return float(mv["doubleValue"])
    return 0.0


def _fetch_network_report_once(publisher_id: str, start: date, end: date) -> list[dict]:
    """(date, app, ad_unit, country) 단위 AdMob 수익. admob_daily 스키마와 동일한 dict 리스트 반환."""
    service = _get_service()

    report_spec = {
        "date_range": {"start_date": _date_dict(start), "end_date": _date_dict(end)},
        "dimensions": ["DATE", "APP", "AD_UNIT", "COUNTRY"],
        "metrics": ["ESTIMATED_EARNINGS", "AD_REQUESTS", "MATCHED_REQUESTS", "IMPRESSIONS", "CLICKS"],
    }
    request = {"report_spec": report_spec}

    try:
        response = (
            service.accounts()
            .networkReport()
            .generate(parent=f"accounts/{publisher_id}", body=request)
            .execute()
        )
    except HttpError as e:
        raise RuntimeError(f"AdMob API 오류: {e}") from e

    currency = "USD"
    rows: list[dict] = []

    for item in response:
        if "header" in item:
            currency = item["header"].get("localizationSettings", {}).get("currencyCode", currency)
            continue
        row = item.get("row")
        if not row:
            continue  # header/footer 등 row가 없는 항목은 건너뜀

        dims = row.get("dimensionValues", {})
        metric_values = row.get("metricValues", {})

        raw_date = dims.get("DATE", {}).get("value", "")
        iso_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else raw_date

        app_id = dims.get("APP", {}).get("value", "unknown")
        app_name = dims.get("APP", {}).get("displayLabel") or app_id
        ad_unit_id = dims.get("AD_UNIT", {}).get("value", "unknown")
        country = dims.get("COUNTRY", {}).get("value", "UNKNOWN")

        ad_requests = _metric_value(metric_values.get("AD_REQUESTS", {}))
        matched_requests = _metric_value(metric_values.get("MATCHED_REQUESTS", {}))
        impressions = _metric_value(metric_values.get("IMPRESSIONS", {}))
        clicks = _metric_value(metric_values.get("CLICKS", {}))
        earnings = _metric_value(metric_values.get("ESTIMATED_EARNINGS", {}))

        rows.append({
            "date": iso_date,
            "app_id": app_id,
            "app_name": app_name,
            "ad_unit_id": ad_unit_id,
            "country": country,
            "currency": currency,
            "ad_requests": int(ad_requests),
            "matched_requests": int(matched_requests),
            "impressions": int(impressions),
            "clicks": int(clicks),
            "estimated_earnings": round(earnings, 4),
            "ecpm": round((earnings / impressions) * 1000, 4) if impressions else 0.0,
            "match_rate": round(matched_requests / ad_requests, 4) if ad_requests else 0.0,
        })

    return rows


def fetch_network_report(publisher_id: str, start: date, end: date) -> list[dict]:
    """Fetch a report, rebuilding the cached client once after a transport failure."""
    try:
        return _fetch_network_report_once(publisher_id, start, end)
    except RuntimeError as exc:
        if not _is_transient_transport_error(exc):
            raise
        logger.warning("AdMob transport error; rebuilding cached client and retrying once: %s", exc)
        _get_service.cache_clear()
        return _fetch_network_report_once(publisher_id, start, end)


def extract_app_catalog(rows: list[dict]) -> list[dict]:
    """rows에서 앱 카탈로그(apps 테이블 upsert용)만 추출. package_name/active는 포함하지 않는다."""
    seen: dict[str, dict] = {}
    for row in rows:
        seen[row["app_id"]] = {"app_id": row["app_id"], "app_name": row["app_name"]}
    return list(seen.values())
