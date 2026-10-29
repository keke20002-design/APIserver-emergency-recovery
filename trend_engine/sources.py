"""TrendEngine — 외부 트렌드 소스 집계. blog_topic_engine의 트렌드 수집 분리."""
import logging

logger = logging.getLogger(__name__)


class TrendEngine:
    """HN, Reddit, GitHub Trending 등 외부 소스에서 트렌드 키워드 수집."""

    async def fetch_hn(self) -> list[str]:
        from services.blog_topic_engine import _fetch_hn_trending
        return await _fetch_hn_trending()

    async def fetch_reddit(self) -> list[str]:
        from services.blog_topic_engine import _fetch_reddit_trending
        return await _fetch_reddit_trending()

    async def fetch_all(self) -> list[str]:
        """모든 소스에서 트렌드 수집 후 중복 제거."""
        import asyncio
        hn, reddit = await asyncio.gather(
            self.fetch_hn(),
            self.fetch_reddit(),
            return_exceptions=True,
        )
        results: list[str] = []
        for items in (hn, reddit):
            if isinstance(items, list):
                results.extend(items)
        seen: set[str] = set()
        unique: list[str] = []
        for item in results:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique[:12]
