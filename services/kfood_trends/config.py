from __future__ import annotations

import os

DEFAULT_QUERIES = (
    "두쫀쿠 두바이 쫀득 쿠키 트렌드",
    "SNS 화제 디저트 신메뉴 유행",
    "버터떡 우베 디저트 인기",
    "요즘 인기 한국 간식 디저트",
)


def search_queries() -> tuple[str, ...]:
    raw = os.getenv("KFOOD_TREND_SEARCH_QUERIES", "")
    values = tuple(value.strip() for value in raw.split("|") if value.strip())
    return values or DEFAULT_QUERIES


LOOKBACK_DAYS = 14
MAX_CANDIDATES = 5
MIN_CANDIDATES = 3
NEWS_PER_QUERY = 20
