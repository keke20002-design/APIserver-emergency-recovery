"""TrendLab Collector — 네이버 뉴스 + YouTube 트렌딩 + 주요 채널 수집 (rich metadata)."""
import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from app.trendlab import freshness

logger = logging.getLogger(__name__)

_NAVER_QUERIES = [
    # 범용 뉴스보다 대중 관심도가 높은 연예/대중문화 사건을 충분히 확보한다.
    # 최종 후보는 analyzer의 source-relevance 검증을 통과한 실제 보도만 사용한다.
    "연예 화제", "아이돌 컴백", "배우 드라마 예능", "열애 결혼 이혼 논란",
    "연예인 신곡 신작", "스타 폭로 고소", "스포츠 화제", "게임 화제",
]

_YT_CHANNEL_NAMES = [
    "SBS 뉴스", "KBS News", "MBC 뉴스", "JTBC 뉴스",
    "슈카월드", "침착맨",
]

_YT_CHANNEL_ID_CACHE: dict[str, str] = {}


async def _yt_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    r = await client.get(url, params={**params, "key": api_key}, timeout=12)
    r.raise_for_status()
    return r.json()


async def _resolve_channel_id(client: httpx.AsyncClient, name: str) -> str | None:
    if name in _YT_CHANNEL_ID_CACHE:
        return _YT_CHANNEL_ID_CACHE[name]
    try:
        data = await _yt_get(client, "https://www.googleapis.com/youtube/v3/search", {
            "part": "snippet", "q": name, "type": "channel", "maxResults": 1,
        })
        items = data.get("items", [])
        if not items:
            return None
        channel_id = items[0]["snippet"]["channelId"]
        _YT_CHANNEL_ID_CACHE[name] = channel_id
        return channel_id
    except Exception as e:
        logger.warning("Channel resolve failed '%s': %s", name, e)
        return None


def _parse_yt_item(item: dict, source: str) -> dict:
    """YouTube API 응답 item → 표준 dict. published_at은 UTC datetime 파싱 실패 시 None
    (알 수 없는 날짜를 최신으로 간주하지 않는다 — freshness.parse_to_utc 참고)."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    pub_dt = freshness.parse_to_utc(snippet.get("publishedAt", ""))
    now = datetime.now(timezone.utc)
    hours_ago = (now - pub_dt).total_seconds() / 3600 if pub_dt else None
    video_id = item.get("id", "")

    return {
        "source": source,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", "")[:300],
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        "view_count": int(stats.get("viewCount", 0)),
        "like_count": int(stats.get("likeCount", 0)),
        "published_at": pub_dt.isoformat() if pub_dt else None,
        "hours_ago": round(hours_ago, 1) if hours_ago is not None else None,
        "uploaded_today": hours_ago is not None and hours_ago <= 24,
    }


def _filter_collectible(items: list[dict], label: str) -> list[dict]:
    """published_at이 없거나(invalid) 미래거나(future) 7일 초과(expired)인 아이템을
    TrendLab 후보에서 완전히 제외한다. 48시간~7일 사이는 남기되 점수 단계에서 감점된다."""
    now = datetime.now(timezone.utc)
    kept, dropped = [], {"invalid": 0, "future": 0, "expired": 0}
    for i in items:
        pub_dt = freshness.parse_to_utc(i.get("published_at"))
        tier = freshness.classify(pub_dt, now)
        if freshness.is_collectible(pub_dt, now):
            kept.append(i)
        else:
            dropped[tier if tier in dropped else "invalid"] = dropped.get(tier, 0) + 1
    if any(dropped.values()):
        logger.info(
            "%s freshness filter: kept=%d dropped(invalid=%d future=%d expired=%d)",
            label, len(kept), dropped["invalid"], dropped["future"], dropped["expired"],
        )
    return kept


async def collect_youtube_trending(client: httpx.AsyncClient) -> list[dict]:
    """YouTube 인기급상승(KR) — snippet + statistics 포함."""
    try:
        data = await _yt_get(client, "https://www.googleapis.com/youtube/v3/videos", {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": "KR",
            "maxResults": 50,
            "hl": "ko",
        })
        items = [_parse_yt_item(i, "youtube_trending") for i in data.get("items", [])]
        items = _filter_collectible(items, "YouTube trending")
        logger.info("YouTube trending: %d videos", len(items))
        return items
    except Exception as e:
        logger.warning("YouTube trending failed: %s", e)
        return []


async def collect_youtube_channels(client: httpx.AsyncClient) -> list[dict]:
    """주요 채널 최신 영상 30개 — 제목+설명 수집 (통계는 별도 fetch)."""
    results: list[dict] = []
    for name in _YT_CHANNEL_NAMES:
        channel_id = await _resolve_channel_id(client, name)
        if not channel_id:
            continue
        try:
            # 최신 영상 ID 목록
            search_data = await _yt_get(client, "https://www.googleapis.com/youtube/v3/search", {
                "part": "snippet",
                "channelId": channel_id,
                "order": "date",
                "maxResults": 30,
                "type": "video",
            })
            video_ids = [
                i["id"]["videoId"]
                for i in search_data.get("items", [])
                if i.get("id", {}).get("videoId")
            ]
            if not video_ids:
                continue

            # 통계 포함해서 재조회
            stats_data = await _yt_get(client, "https://www.googleapis.com/youtube/v3/videos", {
                "part": "snippet,statistics",
                "id": ",".join(video_ids[:30]),
            })
            items = [_parse_yt_item(i, "youtube_channel") for i in stats_data.get("items", [])]
            results.extend(items)
            logger.info("Channel '%s': %d videos", name, len(items))
        except Exception as e:
            logger.warning("Channel '%s' failed: %s", name, e)
    return _filter_collectible(results, "YouTube channels")


async def collect_naver_news() -> list[dict]:
    """네이버 뉴스 광범위 수집 — publisher 도메인 포함."""
    from services.naver_api import search_news
    from urllib.parse import urlparse
    import re

    tasks = [search_news(q, display=30, sort="date") for q in _NAVER_QUERIES]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    items: list[dict] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc)
    for r in raw_results:
        if not isinstance(r, list):
            continue
        for a in r:
            title = re.sub(r"<[^>]+>", "", a.get("title", "")).strip()
            if not title or title in seen:
                continue
            seen.add(title)
            desc = re.sub(r"<[^>]+>", "", a.get("description", "")).strip()
            original = a.get("originallink", "") or a.get("link", "")
            try:
                domain = urlparse(original).netloc.replace("www.", "")
            except Exception:
                domain = ""
            pub_dt = freshness.parse_to_utc(a.get("pub_date", ""))
            hours_ago = (now - pub_dt).total_seconds() / 3600 if pub_dt else None
            items.append({
                "source": "naver",
                "title": title,
                "description": desc[:200],
                "url": original,
                "view_count": 0,
                "published_at": pub_dt.isoformat() if pub_dt else None,
                "hours_ago": round(hours_ago, 1) if hours_ago is not None else None,
                "uploaded_today": hours_ago is not None and hours_ago <= 24,
                "publisher": domain,
            })
    items = _filter_collectible(items, "Naver news")
    logger.info("Naver news: %d articles (deduped, freshness-filtered)", len(items))
    return items


async def collect_all() -> list[dict]:
    """전체 수집 — 모든 소스를 통합 list[dict]로 반환."""
    async with httpx.AsyncClient() as client:
        naver, trending, channels = await asyncio.gather(
            collect_naver_news(),
            collect_youtube_trending(client),
            collect_youtube_channels(client),
        )
    all_items = naver + trending + channels
    logger.info("Total collected: %d items (naver=%d, trending=%d, channels=%d)",
                len(all_items), len(naver), len(trending), len(channels))
    return all_items
