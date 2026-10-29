"""Naver Publisher — READY 상태로 DB 저장 (수동 발행용)."""
import logging

from publisher.base import PostPayload, PublishResult, Publisher

logger = logging.getLogger(__name__)


class NaverPublisher(Publisher):
    platform = "naver"

    async def publish(self, payload: PostPayload) -> PublishResult:
        from services.blog_db import save_post_log

        try:
            post_id = save_post_log(
                keyword=payload.keyword,
                topic=payload.topic,
                title=payload.title,
                status="READY",
                platform="naver",
                language=payload.language,
                thumbnail_url=payload.thumbnail_url,
                meta_description=payload.meta_description,
                content=payload.content,
                tags=payload.tags,
                summary=payload.summary,
                traffic_score=payload.traffic_score,
                competition=payload.competition,
                trend_level=payload.trend_level,
                score_reason=payload.score_reason,
                ctr_score=payload.ctr_score,
                seo_score=payload.seo_score,
                ai_risk=payload.ai_risk,
                dopamine_score=payload.dopamine_score,
            )
            logger.info("Naver READY post saved — id=%s title=%s", post_id, payload.title)
            return PublishResult(
                platform="naver",
                status="READY",
                post_id=str(post_id),
                url=None,
            )
        except Exception as e:
            logger.error("Naver save failed: %s", e)
            return PublishResult(platform="naver", status="FAILED", error=str(e))
