"""소상공인시장진흥공단 상가(상권)정보 API - 반경 검색 기반 가맹점(상가) 조회.

locakcurapp 가맹점 지도의 실데이터 소스. 좌표 반경 내 상가를 대/중/소분류
코드로 필터링해 가져온 뒤 음식점/카페/약국/마트 4개 카테고리로 분류한다.

대/중/소분류 코드는 data.go.kr 문서에 정확한 스펙이 없어 실제 API 응답을
1000건 표본 조회해 실측으로 확인했다(2026-09):
    I2      대분류 "음식" (레스토랑 + 카페 전부 포함)
    I212    중분류 "비알코올" (카페/커피 - I2 안에서 카페만 분리하는 데 사용)
    G21501  소분류 "약국"
    G204    중분류 "종합 소매" (슈퍼마켓/편의점 외에 다른 소분류도 섞여있어
             G20404/G20405로 한 번 더 거른다)

한국조폐공사_모바일_가맹점기본정보(지역화폐 가맹점 여부, 취급 상품권 종류)는
data.go.kr 활용신청이 아직 승인되지 않아(NO_OPENAPI_SERVICE_ERROR) 연동을
보류했다 - 승인되면 accepted_vouchers를 채울 수 있다. 그 전까지 이 서비스가
돌려주는 accepted_vouchers/is_open/payment_available은 실데이터가 아니라
플레이스홀더다.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from utils.cache import cache

logger = logging.getLogger(__name__)

STORE_RADIUS_URL = "https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInRadius"
PAGE_SIZE = 200
CACHE_TTL = 1800  # 상가 정보는 자주 안 바뀌므로 30분 캐시

CATEGORY_FOOD_LCLS = "I2"
CATEGORY_CAFE_MCLS = "I212"
CATEGORY_PHARMACY_SCLS = "G21501"
CATEGORY_MART_MCLS = "G204"
MART_SCLS_CODES = {"G20404", "G20405"}  # 슈퍼마켓, 편의점

_RATE_LIMIT = asyncio.Semaphore(3)


def _classify(item: dict) -> str | None:
    if item["indsLclsCd"] == CATEGORY_FOOD_LCLS:
        return "CAFE" if item["indsMclsCd"] == CATEGORY_CAFE_MCLS else "RESTAURANT"
    if item["indsSclsCd"] == CATEGORY_PHARMACY_SCLS:
        return "PHARMACY"
    if item["indsMclsCd"] == CATEGORY_MART_MCLS and item["indsSclsCd"] in MART_SCLS_CODES:
        return "MART"
    return None


async def _fetch_group(
    client: httpx.AsyncClient,
    api_key: str,
    lat: float,
    lng: float,
    radius: int,
    extra_params: dict,
) -> list[dict]:
    params = {
        "serviceKey": api_key,
        "pageNo": 1,
        "numOfRows": PAGE_SIZE,
        "type": "json",
        "cx": lng,
        "cy": lat,
        "radius": radius,
        **extra_params,
    }
    async with _RATE_LIMIT:
        response = None
        for attempt in range(3):
            response = await client.get(STORE_RADIUS_URL, params=params)
            if response.status_code != 429:
                break
            delay = 2.0 * (attempt + 1)
            logger.warning("storeListInRadius 429, %.1fs 후 재시도", delay)
            await asyncio.sleep(delay)
        response.raise_for_status()
    data = response.json()
    return data.get("body", {}).get("items") or []


async def fetch_nearby_merchants(
    lat: float,
    lng: float,
    category: str = "ALL",
    radius: int = 1000,
) -> list[dict]:
    """category: ALL/RESTAURANT/CAFE/PHARMACY/MART."""
    api_key = os.getenv("DATA_GO_KR_SERVICE_KEY")
    if not api_key:
        raise RuntimeError("DATA_GO_KR_SERVICE_KEY environment variable is required")

    cache_key = f"merchants:{round(lat, 3)}:{round(lng, 3)}:{radius}:{category}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # 카테고리별로 서버 필터를 걸어 불필요한 업종(법률/부동산 등) 트래픽을 줄인다.
    groups: list[dict] = []
    if category in ("ALL", "RESTAURANT", "CAFE"):
        groups.append({"indsLclsCd": CATEGORY_FOOD_LCLS})
    if category in ("ALL", "PHARMACY"):
        groups.append({"indsSclsCd": CATEGORY_PHARMACY_SCLS})
    if category in ("ALL", "MART"):
        groups.append({"indsMclsCd": CATEGORY_MART_MCLS})

    async with httpx.AsyncClient(timeout=20.0) as client:
        raw_items: list[dict] = []
        for extra_params in groups:
            raw_items.extend(
                await _fetch_group(client, api_key, lat, lng, radius, extra_params)
            )

    merchants: list[dict] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if item["bizesId"] in seen_ids:
            continue
        seen_ids.add(item["bizesId"])
        mapped_category = _classify(item)
        if mapped_category is None or (category != "ALL" and mapped_category != category):
            continue
        merchants.append(
            {
                "id": item["bizesId"],
                "name": item["bizesNm"],
                "category": mapped_category,
                "latitude": item["lat"],
                "longitude": item["lon"],
                "road_address": item.get("rdnmAdr") or item.get("lnoAdr") or "",
                # KOMSCO 가맹점 API 연동 전까지 플레이스홀더.
                "accepted_vouchers": [],
                "is_open": True,
                "payment_available": True,
            }
        )

    cache.set(cache_key, merchants, ttl=CACHE_TTL)
    return merchants
