"""TrendLab Freshness — 날짜 파싱 및 신선도 판정 공통 유틸.

수집(collector) / 분석(analyzer) / 점수(virality) 3단계에서 공통으로 사용해,
오래된 뉴스가 "오늘의 트렌드"로 잘못 분류되는 것을 막는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# 미래 허용 오차 — 소스 서버 시계 오차 등으로 인한 사소한 미래 시각은 봐주되,
# 그 이상 미래인 published_at은 이상 데이터로 취급한다.
FUTURE_TOLERANCE = timedelta(hours=6)

# 이 기간을 넘긴 뉴스는 "오늘의 이슈" 후보에서 완전히 제외한다.
HARD_CUTOFF = timedelta(days=7)

_TIER_BOUNDS = (
    ("recent6h", timedelta(hours=6)),
    ("recent24h", timedelta(hours=24)),
    ("recent48h", timedelta(hours=48)),
    ("aged7d", HARD_CUTOFF),
)

_FRESHNESS_MULTIPLIER = {
    "recent6h": 1.5,
    "recent24h": 1.3,
    "recent48h": 1.0,
    "aged7d": 0.5,
}

# 후보로 남길 수 있는 tier (수집 단계 필터 기준)
_COLLECTIBLE_TIERS = {"recent6h", "recent24h", "recent48h", "aged7d"}

# "최근"으로 집계할 tier (recent_source_count 기준 — 48시간 이내)
_RECENT_TIERS = {"recent6h", "recent24h", "recent48h"}


def parse_to_utc(raw: str | datetime | None) -> datetime | None:
    """RSS/Naver(RFC2822)/YouTube(ISO8601) 등 소스별로 다른 날짜 포맷을 UTC datetime으로 통일.
    파싱 실패 시 None — 절대 "현재 시각"으로 대체하지 않는다 (알 수 없는 날짜를 최신으로 단정 금지)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    raw = raw.strip()
    if not raw:
        return None

    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            dt = None

    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify(pub_dt: datetime | None, now: datetime | None = None) -> str:
    """'invalid' | 'future' | 'recent6h' | 'recent24h' | 'recent48h' | 'aged7d' | 'expired'"""
    if pub_dt is None:
        return "invalid"
    now = now or datetime.now(timezone.utc)
    if pub_dt > now + FUTURE_TOLERANCE:
        return "future"
    age = now - pub_dt
    for tier, bound in _TIER_BOUNDS:
        if age <= bound:
            return tier
    return "expired"


def is_collectible(pub_dt: datetime | None, now: datetime | None = None) -> bool:
    """수집 단계 후보 여부. invalid/future/expired(7일 초과)는 제외.
    48시간~7일 사이는 후보로 남기되, freshness_multiplier로 점수 단계에서 감점한다."""
    return classify(pub_dt, now) in _COLLECTIBLE_TIERS


def is_recent(pub_dt: datetime | None, now: datetime | None = None) -> bool:
    """48시간 이내 여부 — recent_source_count 집계 및 최종 안전장치 기준."""
    return classify(pub_dt, now) in _RECENT_TIERS


def freshness_multiplier(pub_dt: datetime | None, now: datetime | None = None) -> float:
    """Virality 점수에 곱하는 신선도 가중치. invalid/future/expired는 0 (애초에 후보에서 제외 대상)."""
    return _FRESHNESS_MULTIPLIER.get(classify(pub_dt, now), 0.0)


def freshness_label(pub_dt: datetime | None, now: datetime | None = None) -> str:
    """관리자 화면 표시용 — 🔥 6시간 이내 / 🟢 48시간 이내 / 🟡 48시간~7일. 그 외는 빈 문자열."""
    now = now or datetime.now(timezone.utc)
    tier = classify(pub_dt, now)
    if tier not in _COLLECTIBLE_TIERS:
        return ""
    icon = "🔥" if tier == "recent6h" else ("🟢" if tier in ("recent24h", "recent48h") else "🟡")
    hours = (now - pub_dt).total_seconds() / 3600
    if hours < 1:
        return f"{icon} 방금 전"
    if hours < 24:
        return f"{icon} {int(hours)}시간 전"
    return f"{icon} {int(hours // 24)}일 전"
