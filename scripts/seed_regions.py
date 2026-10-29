"""locakcurapp regions 테이블 시딩 스크립트.

행정안전부 법정동코드(StanReginCd) 공공데이터포털 API에서 실시간으로 전체
시/도·시/군/구 목록을 받아와 Supabase regions 테이블에 upsert한다.
(수동 CSV 다운로드 불필요 - services/mois_region_api.py가 라이브 API를 호출)

사용법:
    python scripts/seed_regions.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from services import mois_region_api, region_service  # noqa: E402


async def main() -> None:
    print("행정안전부 법정동코드 API에서 지역 목록을 가져오는 중...")
    rows = await mois_region_api.fetch_regions_for_seed()
    print(f"시/군/구 {len(rows)}건 수신 완료. Supabase에 upsert 중...")

    batch_size = 200
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        total += await region_service.upsert_regions(batch)
    print(f"{total}건 upsert 완료 (수신 {len(rows)}건).")


if __name__ == "__main__":
    asyncio.run(main())
