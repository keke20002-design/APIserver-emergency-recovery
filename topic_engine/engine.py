"""TopicEngine — blog_topic_engine.py 인터페이스 래퍼."""
import logging

logger = logging.getLogger(__name__)


class TopicEngine:
    """주제 선정 엔진. pick() 호출로 (keyword, category) 반환."""

    def __init__(self, lang: str = "ko"):
        self.lang = lang

    async def pick(
        self,
        exclude_topics: list[str] | None = None,
        exclude_categories: set[str] | None = None,
    ) -> tuple[str, str]:
        """최적 키워드와 카테고리를 반환."""
        from services.blog_topic_engine import pick_topic
        kw, cat = await pick_topic(
            lang=self.lang,
            exclude_topics=exclude_topics or [],
            exclude_categories=exclude_categories or set(),
        )
        logger.debug("TopicEngine.pick — lang=%s kw=%s cat=%s", self.lang, kw, cat)
        return kw, cat

    async def pick_for_naver(
        self,
        exclude_topics: list[str] | None = None,
    ) -> tuple[str, str]:
        """Naver 전용 주제 선정 (KR 한정)."""
        from services.blog_topic_engine import pick_topic
        kw, cat = await pick_topic(
            lang="ko",
            exclude_topics=exclude_topics or [],
        )
        return kw, cat
