"""WordPress Publisher — blog_wordpress_service.py 위임."""
import logging
from datetime import datetime

import pytz

from publisher.base import PostPayload, PublishResult, Publisher

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


class WordPressPublisher(Publisher):
    platform = "wordpress"

    def __init__(self, wp_status: str = "publish"):
        self.wp_status = wp_status

    async def publish(self, payload: PostPayload) -> PublishResult:
        from services.blog_wordpress_service import publish_post, upload_image

        featured_image_id = payload.featured_image_id
        if payload.img_bytes and payload.img_mime and not featured_image_id:
            try:
                ext = "jpg" if "jpeg" in payload.img_mime else payload.img_mime.split("/")[-1]
                filename = f"post-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}.{ext}"
                upload = await upload_image(payload.img_bytes, payload.img_mime, filename)
                raw_id = upload.get("id")
                if raw_id:
                    featured_image_id = int(raw_id)
            except Exception as e:
                logger.warning("WP image upload failed: %s", e)

        try:
            result = await publish_post(
                title=payload.title,
                content=payload.content,
                tags=payload.tags,
                category=payload.topic,
                meta_description=payload.meta_description,
                status=self.wp_status,
                featured_image_id=featured_image_id,
            )
            return PublishResult(
                platform="wordpress",
                status="PUBLISHED" if self.wp_status == "publish" else "READY",
                post_id=str(result["post_id"]),
                url=result["url"],
            )
        except Exception as e:
            return PublishResult(platform="wordpress", status="FAILED", error=str(e))
