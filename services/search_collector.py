import asyncio
import logging
from typing import Literal

from services.naver_api import search_blog

logger = logging.getLogger(__name__)

# 구장 단축키 → 네이버 검색용 이름
STADIUM_SEARCH_NAMES: dict[str, str] = {
    "잠실": "잠실야구장",
    "고척": "고척스카이돔",
    "문학": "인천SSG랜더스필드",
    "수원": "수원KT위즈파크",
    "사직": "사직야구장",
    "대전": "대전한화생명이글스파크",
    "대구": "대구삼성라이온즈파크",
    "광주": "광주기아챔피언스필드",
    "창원": "창원NC파크",
}

# 구장 단축키 → 앱 DB 저장용 풀네임 (BaseStadium 앱의 stadium.name 과 일치)
STADIUM_APP_NAMES: dict[str, str] = {
    "잠실": "잠실야구장",
    "고척": "고척스카이돔",
    "문학": "인천 SSG 랜더스필드",
    "수원": "수원 KT 위즈파크",
    "사직": "사직야구장",
    "대전": "한화생명 이글스파크",
    "대구": "대구 삼성 라이온즈파크",
    "광주": "광주-기아 챔피언스필드",
    "창원": "창원 NC 파크",
}

# 하위 호환용 alias
STADIUM_NAMES = STADIUM_SEARCH_NAMES

UpdateType = Literal["parking", "restaurant", "food", "traffic", "notice", "tip"]

# 타입별 검색 쿼리 템플릿 ({name} 자리에 구장 전체명 삽입)
SEARCH_QUERIES: dict[str, list[str]] = {
    "parking":    ["{name} 주차", "{name} 주차장"],
    "restaurant": ["{name} 맛집"],
    "food":       ["{name} 먹거리", "{name} 구장 음식"],
    "traffic":    ["{name} 교통", "{name} 대중교통"],
    "notice":     ["{name} 공지 변경"],
    "tip":        ["{name} 관람팁"],
}


async def _fetch_one(stadium: str, update_type: str, query: str) -> dict:
    try:
        items = await search_blog(query, display=5)
        texts = [item["text"] for item in items]
        titles = [item["title"] for item in items if item.get("title")]
        links = [item["link"] for item in items if item.get("title")]  # titles와 1:1 대응
        logger.info("Collected %d items | %s | %s", len(texts), query, update_type)
        return {"stadium": stadium, "type": update_type, "texts": texts, "titles": titles, "links": links}
    except Exception as e:
        logger.error("Search failed for '%s': %s", query, e)
        return {"stadium": stadium, "type": update_type, "texts": [], "titles": [], "links": []}


async def collect_stadium_updates(stadium_key: str) -> list[dict]:
    """한 야구장에 대해 모든 타입의 네이버 블로그 검색 결과를 수집."""
    search_name = STADIUM_SEARCH_NAMES.get(stadium_key, f"{stadium_key}야구장")
    app_name = STADIUM_APP_NAMES.get(stadium_key, search_name)

    tasks = []
    for update_type, templates in SEARCH_QUERIES.items():
        for template in templates:
            query = template.format(name=search_name)
            tasks.append(_fetch_one(app_name, update_type, query))

    results = await asyncio.gather(*tasks)

    # 같은 타입 결과 병합 (titles/links는 1:1 대응 유지)
    merged_texts: dict[str, list[str]] = {}
    merged_titles: dict[str, list[str]] = {}
    merged_links: dict[str, list[str]] = {}
    for r in results:
        utype = r["type"]
        merged_texts.setdefault(utype, []).extend(r["texts"])
        merged_titles.setdefault(utype, []).extend(r.get("titles", []))
        merged_links.setdefault(utype, []).extend(r.get("links", []))

    return [
        {
            "stadium": app_name,
            "type": utype,
            "texts": merged_texts[utype],
            "titles": merged_titles.get(utype, []),
            "links": merged_links.get(utype, []),
        }
        for utype in merged_texts
    ]


async def collect_all_stadiums() -> list[dict]:
    """모든 야구장 업데이트 수집."""
    tasks = [collect_stadium_updates(k) for k in STADIUM_NAMES]
    nested = await asyncio.gather(*tasks)
    return [item for sublist in nested for item in sublist]
