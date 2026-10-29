"""실시간 환율 조회 (무료, API 키 불필요: open.er-api.com).

plan.md 21번 항목: "임의 환율을 코드에 하드코딩하지 마라" - 매번 실제 API에서 받아온
값만 쓴다. 하루 1회 갱신되는 소스라 6시간 캐시로 충분하다.
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_TTL_SECONDS = 6 * 3600
_cache: dict = {"rates": None, "updated_at": None, "fetched_at": 0.0}


def get_rates(base: str = "USD") -> dict:
    """{"rates": {...}, "updated_at": "..."} 반환. 실패 시 이전 캐시라도 있으면 그걸 반환."""
    now = time.time()
    if _cache["rates"] is not None and now - _cache["fetched_at"] < _TTL_SECONDS:
        return {"rates": _cache["rates"], "updated_at": _cache["updated_at"]}

    try:
        resp = httpx.get(f"https://open.er-api.com/v6/latest/{base}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise RuntimeError(f"unexpected response: {data}")
        _cache["rates"] = data["rates"]
        _cache["updated_at"] = data.get("time_last_update_utc")
        _cache["fetched_at"] = now
    except Exception as e:
        logger.warning("환율 조회 실패: %s", e)
        if _cache["rates"] is None:
            raise

    return {"rates": _cache["rates"], "updated_at": _cache["updated_at"]}
