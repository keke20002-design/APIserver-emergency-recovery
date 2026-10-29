"""일일 배치: 5대 플랫폼 공지 크롤링 -> crawled_notices upsert -> 지난
발행 일정 마감(CLOSED) 처리 -> sync_logs 기록.

main.py에서 매일 새벽 3시(Asia/Seoul) cron으로 이 함수 하나만 호출한다.
"""
from __future__ import annotations

import logging

from services import crawled_notice_service, notice_crawler_service, schedule_service, sync_log_service

logger = logging.getLogger(__name__)


async def run_daily_sync() -> dict:
    notices_collected = 0
    schedules_closed = 0
    error_message = None
    status = "SUCCESS"

    try:
        notices = await notice_crawler_service.crawl_all_platforms()
        notices_collected = await crawled_notice_service.upsert_notices(notices)
        if notices_collected < len(notices):
            status = "PARTIAL"
    except Exception as e:
        logger.error("공지 크롤링 단계 실패: %s", e)
        error_message = f"crawl_failed: {e}"
        status = "PARTIAL"

    try:
        schedules_closed = await schedule_service.close_expired_schedules()
    except Exception as e:
        logger.error("일정 마감 처리 실패: %s", e)
        error_message = (error_message + " | " if error_message else "") + f"close_failed: {e}"
        status = "FAILED" if error_message and "crawl_failed" in error_message else "PARTIAL"

    await sync_log_service.record_run(
        status=status,
        notices_collected=notices_collected,
        schedules_closed=schedules_closed,
        error_message=error_message,
    )
    logger.info(
        "일일 동기화 완료: status=%s notices=%d closed=%d",
        status,
        notices_collected,
        schedules_closed,
    )
    return {
        "status": status,
        "notices_collected": notices_collected,
        "schedules_closed": schedules_closed,
        "error_message": error_message,
    }
