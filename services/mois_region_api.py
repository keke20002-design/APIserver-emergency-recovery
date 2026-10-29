"""행정안전부 법정동코드(StanReginCd) 공공데이터포털 API 클라이언트.

locakcurapp의 regions 테이블(시/군/구 단위)을 채우기 위해 전체 법정동 목록을
받아온 뒤 시/도·시/군/구 레벨 행만 추려낸다. API 자체에 레벨 필터가 없어
(locatadd_nm은 텍스트 부분일치일 뿐) 전체 페이지를 가져와 region_cd 자릿수
패턴(sgg_cd/umd_cd/ri_cd)으로 레벨을 판별한다.

실측(2026-09): 전체 약 20,560행(읍면동/리 단위 포함), numOfRows=1000 기준
21페이지. 시/군/구 레벨만 추리면 250행 내외.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os

import httpx

logger = logging.getLogger(__name__)

STAN_REGINCD_URL = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"
PAGE_SIZE = 1000


def _is_sido_row(row: dict) -> bool:
    return row["sgg_cd"] == "000" and row["umd_cd"] == "000" and row["ri_cd"] == "00"


def _is_sigungu_row(row: dict) -> bool:
    return row["sgg_cd"] != "000" and row["umd_cd"] == "000" and row["ri_cd"] == "00"


# data.go.kr가 짧은 버스트 구간에 민감해 동시요청 몇 개만 겹쳐도 429를 반환한다.
# (실측: 20개 동시요청 -> 다수 429, 순차 + 요청 간 딜레이로는 문제없음)
_REQUEST_INTERVAL = 0.5


async def _fetch_page(
    client: httpx.AsyncClient, api_key: str, page: int
) -> tuple[list[dict], int]:
    params = {
        "ServiceKey": api_key,
        "pageNo": page,
        "numOfRows": PAGE_SIZE,
        "type": "json",
        "flag": "Y",
    }
    for attempt in range(5):
        response = await client.get(STAN_REGINCD_URL, params=params)
        if response.status_code != 429:
            break
        delay = float(response.headers.get("Retry-After") or 3.0 * (attempt + 1))
        logger.warning("StanReginCd 429, %.1fs 후 재시도 (page=%s)", delay, page)
        await asyncio.sleep(delay)
    response.raise_for_status()
    data = response.json()
    payload = data["StanReginCd"]
    total_count = payload[0]["head"][0]["totalCount"]
    rows = payload[1]["row"] if len(payload) > 1 else []
    return rows, total_count


async def fetch_sido_and_sigungu() -> tuple[dict[str, str], list[dict]]:
    """(sido_cd -> sido_name 맵, 시/군/구 레벨 행 목록) 반환.

    일회성 배치 작업이라 속도보다 안정성을 위해 페이지를 순차 조회한다.
    """
    api_key = os.getenv("DATA_GO_KR_SERVICE_KEY")
    if not api_key:
        raise RuntimeError("DATA_GO_KR_SERVICE_KEY environment variable is required")

    async with httpx.AsyncClient(timeout=20.0) as client:
        first_rows, total_count = await _fetch_page(client, api_key, 1)
        all_rows = list(first_rows)

        total_pages = math.ceil(total_count / PAGE_SIZE)
        for page in range(2, total_pages + 1):
            await asyncio.sleep(_REQUEST_INTERVAL)
            rows, _ = await _fetch_page(client, api_key, page)
            all_rows.extend(rows)

    sido_names: dict[str, str] = {}
    sigungu_rows: list[dict] = []
    for row in all_rows:
        if _is_sido_row(row):
            sido_names[row["sido_cd"]] = row["locallow_nm"]
        elif _is_sigungu_row(row):
            sigungu_rows.append(row)

    return sido_names, sigungu_rows


async def fetch_regions_for_seed() -> list[dict]:
    """regions 테이블 upsert용 행 목록.

    각 행: sido_code(2자리)/sido_name/sigungu_code(5자리, sido+sgg)/
    sigungu_name/is_active.
    """
    sido_names, sigungu_rows = await fetch_sido_and_sigungu()
    seed_rows: list[dict] = []
    for row in sigungu_rows:
        sido_cd = row["sido_cd"]
        # 세종특별자치시처럼 하위 구/군이 없는 단일 계층 자치단체는 시/도
        # 레벨 행(sgg_cd=000)이 따로 없어 sido_names에 없다 - 자기 자신의
        # locallow_nm을 시/도명으로도 사용한다 (실측 확인: sido_cd=36).
        sido_name = sido_names.get(sido_cd) or row["locallow_nm"]
        seed_rows.append(
            {
                "sido_code": sido_cd,
                "sido_name": sido_name,
                "sigungu_code": f"{sido_cd}{row['sgg_cd']}",
                "sigungu_name": row["locallow_nm"],
                "is_active": True,
            }
        )
    return seed_rows
