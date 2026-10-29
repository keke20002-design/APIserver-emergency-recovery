from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from services.naver_api import search_news

from .config import LOOKBACK_DAYS, NEWS_PER_QUERY, search_queries
from .schemas import EvidenceArticle

logger = logging.getLogger(__name__)


def _normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _publisher(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return host or "unknown"


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _similar_title(left: str, right: str) -> bool:
    compact_left = "".join(left.lower().split())
    compact_right = "".join(right.lower().split())
    return SequenceMatcher(None, compact_left, compact_right).ratio() >= 0.88


async def collect_recent_news() -> tuple[list[EvidenceArticle], int]:
    queries = search_queries()
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(
            *(search_news(query, display=NEWS_PER_QUERY, sort="date", client=client) for query in queries),
            return_exceptions=True,
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    articles: list[EvidenceArticle] = []
    seen_urls: set[str] = set()
    for query, result in zip(queries, results):
        if isinstance(result, BaseException):
            logger.warning("K-food Naver search failed query=%s: %s", query, result)
            continue
        for item in result:
            published_at = _parse_date(item.get("pub_date", ""))
            if published_at and published_at.astimezone(timezone.utc) < cutoff:
                continue
            url = _normalize_url(item.get("link", ""))
            title = item.get("title", "").strip()
            summary = item.get("description", "").strip()
            if not url or not title or not summary:
                continue
            if url in seen_urls or any(_similar_title(title, row.title) for row in articles):
                continue
            seen_urls.add(url)
            articles.append(EvidenceArticle(
                title=title, url=url, publisher=_publisher(url),
                published_at=published_at, summary=summary,
            ))
    logger.info("K-food news collected: %d articles from %d API calls", len(articles), len(queries))
    return articles, len(queries)
