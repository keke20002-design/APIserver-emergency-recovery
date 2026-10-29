"""WordPress Generator — blog_gemini_service.py 위임."""
import logging

from content_engine.base import ContentGenerator, GeneratedContent

logger = logging.getLogger(__name__)


class WordPressKRGenerator(ContentGenerator):
    """한국어 SEO 가이드 포스트 (WordPress)."""
    platform = "wordpress"
    language = "ko"

    async def generate(self, keyword: str, topic: str, intent: str = "") -> GeneratedContent:
        from services.blog_gemini_service import generate_kr_guide_post as _gen
        data = await _gen(
            recent_keywords=[],
            forced_keyword=keyword,
            forced_category=topic,
        )
        return GeneratedContent(
            keyword=data.get("keyword", keyword),
            title=data["title"],
            content=data["content"],
            meta_description=data.get("meta_description", ""),
            tags=data.get("tags", []),
            summary=data.get("summary", []),
            language="ko",
            platform="wordpress",
            category=data.get("_category", topic),
        )


class WordPressENGenerator(ContentGenerator):
    """영어 How-To 가이드 포스트 (WordPress)."""
    platform = "wordpress"
    language = "en"

    async def generate(self, keyword: str, topic: str, intent: str = "") -> GeneratedContent:
        from services.blog_gemini_service import generate_english_guide_post as _gen
        data = await _gen(
            recent_keywords=[],
            forced_keyword=keyword,
            forced_category=topic,
        )
        return GeneratedContent(
            keyword=data.get("keyword", keyword),
            title=data["title"],
            content=data["content"],
            meta_description=data.get("meta_description", ""),
            tags=data.get("tags", []),
            summary=data.get("summary", []),
            language="en",
            platform="wordpress",
            category=data.get("_category", topic),
        )
