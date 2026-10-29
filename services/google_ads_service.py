"""Google Ads API 연동 — 캠페인/국가/일자별 실제 성과 데이터 수집.

인증: .env의 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN /
GOOGLE_ADS_DEVELOPER_TOKEN 사용 (OAuth Desktop app + refresh token 방식).
refresh token은 generate_refresh_token.py로 최초 1회 발급.

중요: 캠페인이 어떤 앱(app_id)에 속하는지는 Google Ads API가 알려주지 않는다.
새로 발견된 캠페인은 campaigns 테이블에 app_id=NULL로 upsert되고, 관리자가
Settings > App Mapping에서 수동으로 채워야 한다 (plan.md #23, 아직 미구현).
"""
from __future__ import annotations

import logging
import os
from datetime import date
from functools import lru_cache

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from services import fx_service

logger = logging.getLogger(__name__)

API_VERSION = "v25"


class GoogleAdsNotConfigured(Exception):
    """필수 환경변수가 비어있음."""


class GoogleAdsAccessDenied(Exception):
    """개발자 토큰이 Test account access 등급이거나 계정 접근 권한이 없음."""


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise GoogleAdsNotConfigured(f"{name} 환경변수가 설정되지 않았습니다.")
    return value


def _to_usd(amount: float, account_currency: str) -> float:
    """대시보드 전체가 'DB에 저장된 금액은 항상 USD'를 전제로 하므로(useCurrency.ts 참고),
    Google Ads 계정 통화(예: 이 계정은 KRW)로 온 cost를 저장 전에 USD로 환산해둔다.
    안 하면 프런트엔드가 이미 KRW인 값을 다시 KRW로 환산해 실제보다 훨씬 크게 보인다."""
    if account_currency == "USD" or amount == 0:
        return amount
    rate = fx_service.get_rates(base="USD")["rates"].get(account_currency)
    if not rate:
        logger.warning("환율 정보 없음(%s), USD 환산 없이 원본값 사용", account_currency)
        return amount
    return round(amount / rate, 4)


@lru_cache(maxsize=1)
def _get_client() -> GoogleAdsClient:
    config = {
        "developer_token": _required_env("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": _required_env("GOOGLE_CLIENT_ID"),
        "client_secret": _required_env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": _required_env("GOOGLE_REFRESH_TOKEN"),
        "use_proto_plus": True,
    }
    # 관리자(MCC) 계정 밑에 하위 계정으로 연결된 경우에만 필요.
    login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_customer_id:
        config["login_customer_id"] = login_customer_id.replace("-", "")
    return GoogleAdsClient.load_from_dict(config, version=API_VERSION)


def _raise_friendly_error(e: GoogleAdsException) -> None:
    messages = [err.message for err in e.failure.errors]
    joined = "; ".join(messages) or str(e)
    lowered = joined.lower()
    if "developer token" in lowered or "not approved" in lowered or "test account" in lowered:
        raise GoogleAdsAccessDenied(
            "개발자 토큰이 아직 Test account access 등급이라 실제 계정 데이터를 조회할 수 없습니다. "
            "Google Ads > API 센터에서 기본 액세스(Basic Access) 승인을 신청하세요. "
            f"(원본 에러: {joined})"
        ) from e
    raise RuntimeError(f"Google Ads API 오류: {joined}") from e


def _resolve_country_codes(client: GoogleAdsClient, customer_id: str, criterion_ids: set[int]) -> dict[int, str]:
    """geographic_view.country_criterion_id -> ISO 국가코드 (geo_target_constant 조회, CSV/하드코딩 없이 API로 직접 확인)."""
    if not criterion_ids:
        return {}
    ga_service = client.get_service("GoogleAdsService")
    resource_names = ", ".join(f"'geoTargetConstants/{cid}'" for cid in criterion_ids)
    query = f"""
        SELECT geo_target_constant.id, geo_target_constant.country_code
        FROM geo_target_constant
        WHERE geo_target_constant.resource_name IN ({resource_names})
    """
    mapping: dict[int, str] = {}
    try:
        stream = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                mapping[row.geo_target_constant.id] = row.geo_target_constant.country_code
    except GoogleAdsException as e:
        logger.warning("geo_target_constant 조회 실패, country는 UNKNOWN으로 남습니다: %s", e)
    return mapping


# google_ads_daily에서 이 country 값을 가진 행은 실제 국가가 아니라, campaign 단위로만
# 조회 가능한 설치수(metrics.biddable_app_install_conversions)를 담아두는 자리다.
# 국가별 집계(build_country_table, build_recommendations)에서는 반드시 제외해야 하고,
# 캠페인/앱/전체 집계에서는 포함해야 정확한 설치수 합이 나온다.
INSTALL_ONLY_COUNTRY = "_INSTALLS_ONLY"

_INSTALL_COST_GOALS = {
    "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST",
    "OPTIMIZE_IN_APP_CONVERSIONS_TARGET_INSTALL_COST",
}


def _enum_name(value) -> str:
    return getattr(value, "name", str(value).rsplit(".", 1)[-1])


def _target_install_cost_micros(goal: str, target_cpa_micros: int, maximize_cpa_micros: int) -> int | None:
    """설치비 최적화 캠페인의 CPA만 tCPI로 취급한다."""
    if goal not in _INSTALL_COST_GOALS:
        return None
    value = target_cpa_micros or maximize_cpa_micros
    return value if value > 0 else None


def fetch_campaign_statuses(customer_id: str) -> list[dict]:
    """Read current Google Ads lifecycle and serving state, including removed campaigns."""
    service = _get_client().get_service("GoogleAdsService")
    query = """SELECT campaign.id, campaign.name, campaign.status,
        campaign.serving_status, campaign.primary_status, campaign.primary_status_reasons,
        campaign.target_cpa.target_cpa_micros, campaign.maximize_conversions.target_cpa_micros,
        campaign.app_campaign_setting.bidding_strategy_goal_type, campaign.campaign_budget,
        campaign_budget.amount_micros, customer.currency_code, customer.time_zone FROM campaign
        WHERE campaign.status IN ('ENABLED', 'PAUSED', 'REMOVED')"""
    try:
        stream = service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
        rows = [{"campaign_id": str(row.campaign.id), "campaign_name": row.campaign.name,
                 "ads_status": _enum_name(row.campaign.status),
                 "serving_status": _enum_name(row.campaign.serving_status),
                 "primary_status": _enum_name(row.campaign.primary_status),
                 "primary_status_reasons": [_enum_name(v) for v in row.campaign.primary_status_reasons],
                 "target_cpi_native": (_target_install_cost_micros(_enum_name(row.campaign.app_campaign_setting.bidding_strategy_goal_type), row.campaign.target_cpa.target_cpa_micros, row.campaign.maximize_conversions.target_cpa_micros) or 0) / 1_000_000,
                 "currency_code": row.customer.currency_code,
                 "account_timezone": row.customer.time_zone,
                 "budget_resource": row.campaign.campaign_budget,
                 "daily_budget_native": row.campaign_budget.amount_micros / 1_000_000}
                for batch in stream for row in batch.results]
        for row in rows:
            row["current_target_cpi"] = _to_usd(row["target_cpi_native"], row["currency_code"]) if row["target_cpi_native"] > 0 else None
            row["daily_budget"] = _to_usd(row["daily_budget_native"], row["currency_code"])
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(rows[0]["account_timezone"] if rows else "UTC")
        today = datetime.now(zone).date()
        start = today - timedelta(days=29)
        query = f"""SELECT change_event.change_date_time, change_event.campaign,
            change_event.change_resource_name, change_event.change_resource_type,
            change_event.changed_fields FROM change_event
            WHERE change_event.change_date_time >= '{start}'
            AND change_event.change_date_time < '{today + timedelta(days=1)}'
            ORDER BY change_event.change_date_time DESC LIMIT 10000"""
        try:
            events = [r.change_event for b in service.search_stream(customer_id=customer_id.replace("-", ""), query=query) for r in b.results]
            for row in rows:
                target_events = []
                budget_events = []
                for event in events:
                    fields = list(event.changed_fields.paths)
                    stamp = datetime.fromisoformat(event.change_date_time).replace(tzinfo=zone).isoformat()
                    if event.campaign.split("/")[-1] == row["campaign_id"] and any("target_cpa" in field or "bidding_strategy" in field for field in fields):
                        target_events.append(stamp)
                    if event.change_resource_name == row["budget_resource"] and any("amount_micros" in field for field in fields):
                        budget_events.append(stamp)
                row.update(history_verified=len(events) < 10000, history_start=str(start),
                           actual_target_changed_at=target_events[0] if target_events else None,
                           actual_budget_changed_at=budget_events[0] if budget_events else None)
        except Exception:
            logger.exception("Campaign change history lookup failed")
            for row in rows:
                row.update(history_verified=False, history_start=str(start), actual_target_changed_at=None, actual_budget_changed_at=None)
        return rows
    except GoogleAdsException as e:
        _raise_friendly_error(e)


def fetch_campaign_settings(customer_id: str) -> list[dict]:
    """캠페인의 읽기 전용 입찰 목표와 일예산 스냅샷을 조회한다."""
    client = _get_client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = customer_id.replace("-", "")
    query = """
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.app_campaign_setting.bidding_strategy_goal_type,
          campaign.target_cpa.target_cpa_micros,
          campaign.maximize_conversions.target_cpa_micros,
          campaign_budget.amount_micros,
          customer.currency_code
        FROM campaign
        WHERE campaign.status != 'REMOVED'
    """

    results = []
    try:
        stream = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                goal = _enum_name(row.campaign.app_campaign_setting.bidding_strategy_goal_type)
                target_micros = _target_install_cost_micros(
                    goal,
                    row.campaign.target_cpa.target_cpa_micros,
                    row.campaign.maximize_conversions.target_cpa_micros,
                )
                currency = row.customer.currency_code
                results.append({
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "active": _enum_name(row.campaign.status) == "ENABLED",
                    "bidding_strategy_goal_type": goal,
                    "target_cpi": _to_usd(target_micros / 1_000_000, currency) if target_micros else None,
                    "daily_budget": _to_usd(row.campaign_budget.amount_micros / 1_000_000, currency),
                })
    except GoogleAdsException as e:
        _raise_friendly_error(e)
    return results


def fetch_campaign_country_daily(customer_id: str, start: date, end: date) -> list[dict]:
    """(date, campaign, country) 단위 실제 성과 - 비용/클릭/노출/전환수.

    주의: metrics.biddable_app_install_conversions(App 설치수)는 user_location_view /
    geographic_view 등 위치 세분화 리소스와 함께 조회할 수 없다는 Google Ads API 제약이 있다
    ("Cannot select or filter on ... 'biddable_app_install_conversions' ... incompatible with
    the resource in the FROM clause"). 그래서 이 함수는 국가별 행의 installs를 0으로 두고,
    실제 설치수는 fetch_campaign_installs_daily()가 별도로 가져온다.
    """
    client = _get_client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = customer_id.replace("-", "")

    query = f"""
        SELECT
          segments.date,
          campaign.id,
          campaign.name,
          user_location_view.country_criterion_id,
          customer.currency_code,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM user_location_view
        WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
    """

    raw_rows = []
    criterion_ids: set[int] = set()
    try:
        stream = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                raw_rows.append(row)
                criterion_ids.add(row.user_location_view.country_criterion_id)
    except GoogleAdsException as e:
        _raise_friendly_error(e)

    country_map = _resolve_country_codes(client, customer_id, criterion_ids)

    # user_location_view는 국가보다 더 세분화된 위치 criterion을 쓰기 때문에, 서로 다른
    # criterion_id가 같은 country_code로 묶이는 경우가 흔하다(예: 지역 단위 criterion 여러 개가
    # 같은 나라로 해석되거나, 조회 실패로 여러 criterion이 전부 "UNKNOWN"이 되는 경우).
    # (date, campaign_id, country) 기준 unique upsert이므로 여기서 미리 합산하지 않으면
    # "ON CONFLICT DO UPDATE command cannot affect row a second time" 에러가 난다.
    merged: dict[tuple, dict] = {}
    for row in raw_rows:
        country_code = country_map.get(row.user_location_view.country_criterion_id, "UNKNOWN")
        key = (row.segments.date, str(row.campaign.id), country_code)
        bucket = merged.get(key)
        if bucket is None:
            bucket = {
                "date": row.segments.date,
                "account_id": customer_id,
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "app_id": None,  # campaigns 테이블에서 매핑되기 전까지 미배정
                "country": country_code,
                "currency": "USD",  # cost는 아래에서 계정 통화 -> USD로 환산해 저장한다
                "impressions": 0,
                "clicks": 0,
                "cost": 0.0,
                "conversions": 0,
                "installs": 0,
            }
            merged[key] = bucket
        bucket["impressions"] += row.metrics.impressions
        bucket["clicks"] += row.metrics.clicks
        bucket["cost"] = round(bucket["cost"] + row.metrics.cost_micros / 1_000_000, 4)
        bucket["conversions"] += round(row.metrics.conversions)

    account_currency = raw_rows[0].customer.currency_code if raw_rows else "USD"
    results = list(merged.values())
    for r in results:
        r["cost"] = _to_usd(r["cost"], account_currency)
    return results


def fetch_campaign_installs_daily(customer_id: str, start: date, end: date) -> list[dict]:
    """(date, campaign) 단위 실제 설치수. country는 INSTALL_ONLY_COUNTRY 자리표시자로 채워서
    fetch_campaign_country_daily()의 국가별 행과 같은 (date, campaign_id, country) 테이블에
    upsert할 수 있게 한다 (cost/clicks/impressions/conversions는 이미 국가별 행에 있으므로 0)."""
    client = _get_client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = customer_id.replace("-", "")

    query = f"""
        SELECT
          segments.date,
          campaign.id,
          campaign.name,
          customer.currency_code,
          metrics.biddable_app_install_conversions
        FROM campaign
        WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
    """

    results = []
    try:
        stream = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                results.append({
                    "date": row.segments.date,
                    "account_id": customer_id,
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "app_id": None,
                    "country": INSTALL_ONLY_COUNTRY,
                    "currency": row.customer.currency_code,
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0,
                    "conversions": 0,
                    "installs": round(row.metrics.biddable_app_install_conversions),
                })
    except GoogleAdsException as e:
        _raise_friendly_error(e)
    return results


def extract_campaign_catalog(rows: list[dict]) -> list[dict]:
    """rows에서 캠페인 카탈로그(campaigns 테이블 upsert용)만 추출. app_id는 절대 포함하지 않는다
    (포함하면 upsert 시 관리자가 이미 지정한 app_id를 덮어써버림)."""
    seen: dict[str, dict] = {}
    for row in rows:
        seen[row["campaign_id"]] = {
            "campaign_id": row["campaign_id"],
            "campaign_name": row["campaign_name"],
        }
    return list(seen.values())
