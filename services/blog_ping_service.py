"""발행 직후 IndexNow로 Bing/Google에 즉시 색인 요청 전송."""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BLOG_HOST = "blog.kekegroup.uk"
_SITEMAP_URL = f"https://{_BLOG_HOST}/sitemap.xml"


async def ping_search_engines(post_url: str) -> dict:
    """IndexNow(Bing) + Google Search Console XML ping 전송."""
    results: dict = {}
    indexnow_key = os.getenv("INDEXNOW_KEY", "")

    async with httpx.AsyncClient(timeout=10) as client:
        # ── IndexNow (Bing / Yandex 지원, Google도 2023년부터 수용) ──────────
        if indexnow_key:
            try:
                r = await client.post(
                    "https://api.indexnow.org/indexnow",
                    json={
                        "host": _BLOG_HOST,
                        "key": indexnow_key,
                        "keyLocation": f"https://{_BLOG_HOST}/{indexnow_key}.txt",
                        "urlList": [post_url],
                    },
                    headers={"Content-Type": "application/json"},
                )
                results["indexnow"] = r.status_code
                logger.info("IndexNow ping: %d — %s", r.status_code, post_url)
            except Exception as e:
                results["indexnow"] = "error"
                logger.warning("IndexNow ping failed: %s", e)
        else:
            results["indexnow"] = "skipped (INDEXNOW_KEY not set)"
            logger.debug("IndexNow ping skipped — set INDEXNOW_KEY in .env")

        # ── Bing 사이트맵 핑 (fallback) ──────────────────────────────────────
        try:
            r = await client.get(
                f"https://www.bing.com/ping?sitemap={_SITEMAP_URL}",
            )
            results["bing"] = r.status_code
            logger.info("Bing sitemap ping: %d", r.status_code)
        except Exception as e:
            results["bing"] = "error"
            logger.warning("Bing sitemap ping failed: %s", e)

    return results
