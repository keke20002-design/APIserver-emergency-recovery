"""
Google News RSS로 KBO 팀별 스포츠 미디어 뉴스를 실시간 수집합니다.
구단 공식 홈페이지 크롤러(team_news_crawler)와 별개로 동작합니다.
"""
import asyncio
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import httpx
from utils.cache import cache

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# 팀명 → 구글 뉴스 검색 키워드
_TEAM_QUERY_MAP: dict[str, str] = {
    "LG":  "LG트윈스",
    "두산": "두산베어스",
    "SSG": "SSG랜더스",
    "키움": "키움히어로즈",
    "KT":  "KT위즈",
    "KIA": "KIA타이거즈",
    "롯데": "롯데자이언츠",
    "한화": "한화이글스",
    "NC":  "NC다이노스",
    "삼성": "삼성라이온즈",
}

_RFC2822_FMTS = [
    "%a, %d %b %Y %H:%M:%S %Z",
    "%a, %d %b %Y %H:%M:%S %z",
]


def _resolve_query(team_name: str) -> str:
    """팀 이름(혹은 약칭)을 검색 키워드로 변환."""
    for key, keyword in _TEAM_QUERY_MAP.items():
        if key in team_name or team_name in key or team_name in keyword:
            return keyword
    return team_name


def _parse_pub_date(raw: str) -> str:
    """RFC 2822 날짜 → YYYY-MM-DD (KST). 실패 시 빈 문자열."""
    normalized = (raw or "").strip().replace("GMT", "UTC")
    for fmt in _RFC2822_FMTS:
        try:
            dt = datetime.strptime(normalized, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_KST).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


_CACHE_TTL = 3600  # 1시간


async def fetch_sports_news(team_name: str, limit: int = 15) -> list[dict]:
    """
    Google News RSS로 팀 관련 스포츠 뉴스 수집 (30분 캐시).
    team_name: 팀 전체 이름 또는 약칭 (예: 'LG트윈스', 'LG', '한화이글스')
    """
    cache_key = f"sports_news:{team_name}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.debug("sports_news cache hit [%s]", team_name)
        return cached

    keyword = _resolve_query(team_name)
    query = f"{keyword} KBO"
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers=_HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
    except Exception as e:
        logger.error("sports_news RSS fetch failed [%s]: %s", team_name, e)
        return []

    items: list[dict] = []
    seen_titles: set[str] = set()
    for el in root.findall(".//item")[:limit * 2]:  # 중복 여유분 포함하여 더 파싱
        title_raw = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        pub_date = el.findtext("pubDate") or ""
        source_el = el.find("source")
        source = source_el.text.strip() if source_el is not None else ""
        desc = _strip_html(el.findtext("description") or "")

        # "기사 제목 - 언론사명" 형식에서 언론사 분리
        if " - " in title_raw and not source:
            parts = title_raw.rsplit(" - ", 1)
            title = parts[0].strip()
            source = parts[1].strip()
        else:
            title = title_raw

        if not title or not link:
            continue

        # 제목 기준 중복 제거
        if title in seen_titles:
            continue
        seen_titles.add(title)

        items.append({
            "team": team_name,
            "title": title,
            "source": source,
            "published_date": _parse_pub_date(pub_date),
            "url": link,
            "summary": desc[:300] if desc else None,
        })
        if len(items) >= limit:
            break

    logger.info("sports_news [%s]: %d items", team_name, len(items))
    cache.set(cache_key, items, ttl=_CACHE_TTL)
    return items


async def fetch_match_sports_news(
    home_team: str, away_team: str, limit_per_team: int = 15
) -> list[dict]:
    """두 팀의 스포츠 뉴스를 병렬 수집 후 날짜 내림차순으로 합산."""
    home_news, away_news = await asyncio.gather(
        fetch_sports_news(home_team, limit_per_team),
        fetch_sports_news(away_team, limit_per_team),
    )
    combined = [*home_news, *away_news]
    combined.sort(key=lambda x: x.get("published_date", ""), reverse=True)
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in combined:
        title = item.get("title", "")
        if title and title not in seen:
            seen.add(title)
            deduped.append(item)
    return deduped
