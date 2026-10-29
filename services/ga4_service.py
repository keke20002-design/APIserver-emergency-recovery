"""GA4 Data API 연동 - 사용자/세션/광고수익 + Cohort 기반 리텐션 수집.

인증: Google Ads/AdMob과 동일한 OAuth 자격증명 재사용 (scope: analytics.readonly).

주의: GA4 표준 리포트는 (date, country) 단위 new_users/active_users/sessions/
ad_revenue는 정확히 준다. 하지만 리텐션(D1/D7/D30)은 Cohort Report로만 얻을 수
있고, Cohort Report는 국가별로 쪼갤 수 없다(속성 전체 기준 1개 값만 나온다).
그래서 리텐션 값은 country와 무관하게 그날 모든 country row에 동일하게 채운다 -
이건 근사치이며 코드에 명시해둔다 (전체 대비 국가별 편차는 있을 수 있음).
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from functools import lru_cache

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    Cohort,
    Filter,
    FilterExpression,
    CohortsRange,
    CohortSpec,
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

# GA4 Cohort Report는 한 요청에 넣을 수 있는 cohort 개수에 제한이 있다.
# 안전하게 배치 크기를 20일로 제한하고, 필요하면 여러 번 나눠 호출한다.
_MAX_COHORTS_PER_REQUEST = 20


class GA4NotConfigured(Exception):
    """필수 환경변수가 비어있음."""


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise GA4NotConfigured(f"{name} 환경변수가 설정되지 않았습니다.")
    return value


@lru_cache(maxsize=1)
def _get_client() -> BetaAnalyticsDataClient:
    # Google Ads/AdMob 계정과 GA4/Firebase 속성을 소유한 구글 계정이 서로 달라서
    # (AdMob은 AdSense 연동 제약으로 계정을 하나로 합칠 수 없었음) refresh token을 분리했다.
    # GA4_REFRESH_TOKEN이 없으면 예전처럼 GOOGLE_REFRESH_TOKEN으로 폴백한다.
    refresh_token = os.getenv("GA4_REFRESH_TOKEN") or _required_env("GOOGLE_REFRESH_TOKEN")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_required_env("GOOGLE_CLIENT_ID"),
        client_secret=_required_env("GOOGLE_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    return BetaAnalyticsDataClient(credentials=creds)


def _stream_filter(stream_id: str | None) -> FilterExpression | None:
    """한 GA4 속성을 여러 앱이 공유할 때(예: gjump-96d19 = gjump/keysmash/BubblePoP) 앱별 데이터 스트림만 걸러낸다."""
    if not stream_id:
        return None
    return FilterExpression(filter=Filter(field_name="streamId", string_filter=Filter.StringFilter(value=str(stream_id))))


def fetch_standard_report(property_id: str, start: date, end: date, stream_id: str | None = None) -> list[dict]:
    """(date, country) 단위 new_users/active_users/sessions/ad_revenue."""
    client = _get_client()
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimension_filter=_stream_filter(stream_id),
        dimensions=[Dimension(name="date"), Dimension(name="country")],
        metrics=[
            Metric(name="newUsers"),
            Metric(name="activeUsers"),
            Metric(name="sessions"),
            Metric(name="totalAdRevenue"),
        ],
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
    )
    response = client.run_report(request)

    rows = []
    for row in response.rows:
        raw_date = row.dimension_values[0].value  # YYYYMMDD
        iso_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        country = row.dimension_values[1].value or "UNKNOWN"
        rows.append({
            "date": iso_date,
            "country": country,
            "new_users": int(float(row.metric_values[0].value)),
            "active_users": int(float(row.metric_values[1].value)),
            "sessions": int(float(row.metric_values[2].value)),
            "ad_revenue": round(float(row.metric_values[3].value), 4),
        })
    return rows


def _chunk_dates(dates: list[date], size: int) -> list[list[date]]:
    return [dates[i : i + size] for i in range(0, len(dates), size)]


def fetch_retention(property_id: str, start: date, end: date, stream_id: str | None = None) -> dict[str, dict]:
    """설치(첫 세션)일 기준 D1/D7/D30 리텐션. {iso_date: {"retention_d1":.., "retention_d7":.., "retention_d30":..}}."""
    client = _get_client()
    all_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    result: dict[str, dict] = {}

    for batch in _chunk_dates(all_dates, _MAX_COHORTS_PER_REQUEST):
        cohorts = [
            Cohort(name=d.isoformat(), dimension="firstSessionDate", date_range=DateRange(start_date=d.isoformat(), end_date=d.isoformat()))
            for d in batch
        ]
        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimension_filter=_stream_filter(stream_id),
            dimensions=[Dimension(name="cohort"), Dimension(name="cohortNthDay")],
            metrics=[Metric(name="cohortActiveUsers"), Metric(name="cohortTotalUsers")],
            cohort_spec=CohortSpec(
                cohorts=cohorts,
                cohorts_range=CohortsRange(granularity=CohortsRange.Granularity.DAILY, start_offset=0, end_offset=30),
            ),
        )
        response = client.run_report(request)

        for row in response.rows:
            cohort_date = row.dimension_values[0].value  # == Cohort.name (우리가 넣은 iso date)
            nth_day = int(row.dimension_values[1].value)
            active = float(row.metric_values[0].value)
            total = float(row.metric_values[1].value)
            if total <= 0:
                continue
            pct = active / total
            bucket = result.setdefault(cohort_date, {"retention_d1": 0.0, "retention_d7": 0.0, "retention_d30": 0.0})
            if nth_day == 1:
                bucket["retention_d1"] = round(pct, 4)
            elif nth_day == 7:
                bucket["retention_d7"] = round(pct, 4)
            elif nth_day == 30:
                bucket["retention_d30"] = round(pct, 4)

    return result


def fetch_analytics_daily(property_id: str, start: date, end: date, stream_id: str | None = None) -> list[dict]:
    """analytics_daily 스키마와 동일한 dict 리스트. app_id는 호출부에서 채운다.
    보통 속성=앱 1:1이지만, 여러 앱이 한 속성을 공유하면 stream_id로 해당 앱 스트림만 조회한다."""
    standard_rows = fetch_standard_report(property_id, start, end, stream_id)
    try:
        retention_by_date = fetch_retention(property_id, start, end, stream_id)
    except Exception as e:
        logger.warning("GA4 retention(cohort report) 조회 실패, 0으로 채웁니다: %s", e)
        retention_by_date = {}

    rows = []
    for r in standard_rows:
        retention = retention_by_date.get(r["date"], {"retention_d1": 0.0, "retention_d7": 0.0, "retention_d30": 0.0})
        rows.append({**r, **retention})
    return rows
