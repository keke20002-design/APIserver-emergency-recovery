"""App Monetization Dashboard - 수익성 계산 엔진.

Mock 데이터와 실제 Google Ads/AdMob 데이터 양쪽에서 공유하는 순수 계산 함수 모음.
DB나 외부 API에 의존하지 않는다 (테스트/재사용 용이성을 위해).
"""
from __future__ import annotations

from dataclasses import dataclass, field


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if not denominator:
        return default
    return numerator / denominator


def cpi(cost: float, installs: float) -> float:
    """CPI = 광고비 / 설치수"""
    return safe_div(cost, installs)


def ctr(clicks: float, impressions: float) -> float:
    """CTR = Clicks / Impressions"""
    return safe_div(clicks, impressions)


def cpc(cost: float, clicks: float) -> float:
    """CPC = 광고비 / Clicks"""
    return safe_div(cost, clicks)


def ecpm(revenue: float, impressions: float) -> float:
    """eCPM = 광고수익 / (노출수 / 1000)"""
    return safe_div(revenue, impressions / 1000 if impressions else 0)


def match_rate(matched_requests: float, ad_requests: float) -> float:
    return safe_div(matched_requests, ad_requests)


def arpu(revenue: float, users: float) -> float:
    """ARPU = 광고수익 / 사용자수"""
    return safe_div(revenue, users)


def profit(ad_revenue: float, ad_spend: float) -> float:
    """Profit = Ad Revenue - Ad Spend"""
    return ad_revenue - ad_spend


def roas(ad_revenue: float, ad_spend: float) -> float:
    """ROAS(%) = Ad Revenue / Ad Spend * 100"""
    return safe_div(ad_revenue, ad_spend) * 100


def ltv(cohort_revenue: float, cohort_users: float) -> float:
    """LTV = 해당 cohort가 특정 시점까지 발생시킨 광고수익 / cohort 사용자 수"""
    return safe_div(cohort_revenue, cohort_users)


def ltv_to_cpi(ltv_value: float, cpi_value: float) -> float:
    """LTV/CPI - 1 이상이면 광고 확대 근거, 1 미만이면 중단 신호."""
    return safe_div(ltv_value, cpi_value)


def break_even_cpi(ltv_value: float, target_roas_pct: float = 100.0) -> float:
    """Target CPI = LTV / (Target ROAS / 100)

    예: LTV $0.50, Target ROAS 120% -> Target CPI = 0.50 / 1.2 = 0.4167
    """
    target_multiplier = safe_div(target_roas_pct, 100)
    return safe_div(ltv_value, target_multiplier)


@dataclass
class RoasThresholds:
    """Settings에서 조정 가능한 ROAS 상태 판정 임계값 (기본값)."""
    scale_min: float = 120.0
    watch_min: float = 80.0
    optimize_min: float = 50.0


def classify_status(roas_pct: float, thresholds: RoasThresholds | None = None) -> str:
    """ROAS 기준 SCALE / WATCH / OPTIMIZE / STOP 상태 분류."""
    t = thresholds or RoasThresholds()
    if roas_pct >= t.scale_min:
        return "SCALE"
    if roas_pct >= t.watch_min:
        return "WATCH"
    if roas_pct >= t.optimize_min:
        return "OPTIMIZE"
    return "STOP"


@dataclass
class ProfitabilityRow:
    """앱/국가/캠페인 단위로 공유하는 수익성 지표 묶음."""
    ad_spend: float = 0.0
    ad_revenue: float = 0.0
    installs: int = 0
    clicks: int = 0
    impressions: int = 0
    users: int = 0
    cohort_revenue_d30: float = 0.0
    cohort_users_d30: int = 0

    profit: float = field(init=False, default=0.0)
    roas: float = field(init=False, default=0.0)
    cpi: float = field(init=False, default=0.0)
    ctr: float = field(init=False, default=0.0)
    cpc: float = field(init=False, default=0.0)
    arpu: float = field(init=False, default=0.0)
    d30_ltv: float = field(init=False, default=0.0)
    ltv_to_cpi: float = field(init=False, default=0.0)
    status: str = field(init=False, default="STOP")

    def compute(self, thresholds: RoasThresholds | None = None, target_roas_pct: float = 100.0) -> "ProfitabilityRow":
        self.profit = profit(self.ad_revenue, self.ad_spend)
        self.roas = roas(self.ad_revenue, self.ad_spend)
        self.cpi = cpi(self.ad_spend, self.installs)
        self.ctr = ctr(self.clicks, self.impressions)
        self.cpc = cpc(self.ad_spend, self.clicks)
        self.arpu = arpu(self.ad_revenue, self.users)
        self.d30_ltv = ltv(self.cohort_revenue_d30, self.cohort_users_d30)
        self.ltv_to_cpi = ltv_to_cpi(self.d30_ltv, self.cpi)
        self.status = classify_status(self.roas, thresholds)
        return self


def diagnose(
    *,
    name: str,
    roas_pct: float,
    cpi_value: float,
    d30_ltv_value: float,
    target_roas_pct: float = 100.0,
    thresholds: RoasThresholds | None = None,
) -> dict:
    """Rule 기반 진단/추천 한 건 생성 (18. 광고 확대/중단 추천)."""
    status = classify_status(roas_pct, thresholds)
    target_cpi = break_even_cpi(d30_ltv_value, target_roas_pct)

    if status == "SCALE":
        message = "현재 광고비 확대를 고려할 수 있습니다."
    elif status == "WATCH":
        message = "현재 추세를 유지하며 지켜볼 필요가 있습니다."
    elif status == "OPTIMIZE":
        message = "타겟팅/소재 최적화가 필요합니다. CPI를 낮추거나 저성과 세그먼트를 조정하세요."
    else:
        message = "사용자 획득 비용 대비 예상 광고수익이 현저히 낮습니다. 광고 중단을 고려하세요."

    return {
        "name": name,
        "status": status,
        "roas": round(roas_pct, 1),
        "cpi": round(cpi_value, 4),
        "d30_ltv": round(d30_ltv_value, 4),
        "target_cpi": round(target_cpi, 4),
        "cpi_over_target": cpi_value > target_cpi,
        "message": message,
    }
