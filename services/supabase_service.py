import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from supabase import create_client, Client

logger = logging.getLogger(__name__)

TABLE = "stadium_updates"


@lru_cache(maxsize=1)
def _get_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables are required")
    return create_client(url, key)


def _run_sync(fn):
    """동기 Supabase 클라이언트를 스레드 풀에서 실행해 비동기 컨텍스트와 호환."""
    return asyncio.get_event_loop().run_in_executor(None, fn)


async def save_pending_update(update: dict) -> dict | None:
    """stadium_updates 테이블에 pending 상태로 저장."""
    try:
        result = await _run_sync(
            lambda: _get_client().table(TABLE).insert(update).execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("Supabase insert failed: %s", e)
        return None


async def get_pending_updates(
    stadium: str | None = None,
    update_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """pending 상태 업데이트 목록 조회 (최신순)."""
    try:
        def _query():
            q = (
                _get_client()
                .table(TABLE)
                .select("*")
                .eq("status", "pending")
                .order("collected_at", desc=True)
                .limit(limit)
            )
            if stadium:
                q = q.eq("stadium", stadium)
            if update_type:
                q = q.eq("type", update_type)
            return q.execute()

        result = await _run_sync(_query)
        return result.data or []
    except Exception as e:
        logger.error("Supabase query failed: %s", e)
        return []


async def approve_update(update_id: int) -> dict | None:
    """업데이트 승인 처리 (status → approved). 앱 테이블 복사는 DB 트리거가 자동 처리."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = await _run_sync(
            lambda: _get_client()
            .table(TABLE)
            .update({"status": "approved", "approved_at": now})
            .eq("id", update_id)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("Supabase approve failed for id=%s: %s", update_id, e)
        return None


async def reject_update(update_id: int) -> dict | None:
    """업데이트 거절 처리 (status → rejected)."""
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(TABLE)
            .update({"status": "rejected"})
            .eq("id", update_id)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("Supabase reject failed for id=%s: %s", update_id, e)
        return None


TEAM_NEWS_TABLE = "team_news"


async def save_team_news(item: dict) -> bool:
    """team_news 테이블에 pending 상태로 저장. URL 중복이면 건너뜀."""
    try:
        published = item.get("published_date") or datetime.now(timezone.utc).date().isoformat()
        result = await _run_sync(
            lambda: _get_client()
            .table(TEAM_NEWS_TABLE)
            .insert({
                **item,
                "status": "pending",
                "start_date": published,
            })
            .execute()
        )
        return bool(result.data)
    except Exception as e:
        msg = str(e)
        if "duplicate" in msg.lower() or "unique" in msg.lower():
            logger.debug("Duplicate team_news skipped: %s", item.get("url"))
            return False
        logger.error("team_news insert failed: %s", e)
        return False


async def get_pending_team_news(
    team: str | None = None,
    news_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """pending 상태 team_news 조회."""
    try:
        def _query():
            q = (
                _get_client()
                .table(TEAM_NEWS_TABLE)
                .select("*")
                .eq("status", "pending")
                .order("collected_at", desc=True)
                .limit(limit)
            )
            if team:
                q = q.eq("team", team)
            if news_type:
                q = q.eq("type", news_type)
            return q.execute()

        result = await _run_sync(_query)
        return result.data or []
    except Exception as e:
        logger.error("team_news query failed: %s", e)
        return []


async def get_approved_team_news(
    team: str | None = None,
    news_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """approved 상태 team_news 조회 (앱 노출용)."""
    try:
        def _query():
            q = (
                _get_client()
                .table(TEAM_NEWS_TABLE)
                .select("*")
                .eq("status", "approved")
                .order("published_date", desc=True)
                .limit(limit)
            )
            if team:
                q = q.eq("team", team)
            if news_type:
                q = q.eq("type", news_type)
            return q.execute()

        result = await _run_sync(_query)
        return result.data or []
    except Exception as e:
        logger.error("team_news approved query failed: %s", e)
        return []


async def approve_team_news(news_id: int) -> dict | None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = await _run_sync(
            lambda: _get_client()
            .table(TEAM_NEWS_TABLE)
            .update({"status": "approved", "approved_at": now})
            .eq("id", news_id)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("team_news approve failed id=%s: %s", news_id, e)
        return None


async def reject_team_news(news_id: int) -> dict | None:
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(TEAM_NEWS_TABLE)
            .update({"status": "rejected"})
            .eq("id", news_id)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("team_news reject failed id=%s: %s", news_id, e)
        return None


MATCH_ODDS_TABLE = "match_odds"
PREDICTIONS_TABLE = "predictions"
MODEL_VS_MARKET_TABLE = "model_vs_market"


async def upsert_match_odds(row: dict) -> dict | None:
    """match_odds에 upsert (match_id, provider 기준). Odds는 시간에 따라
    바뀌므로 새 값을 덮어쓰되, updated_at은 항상 지금 시각으로 갱신한다."""
    try:
        payload = {**row, "updated_at": datetime.now(timezone.utc).isoformat()}
        result = await _run_sync(
            lambda: _get_client()
            .table(MATCH_ODDS_TABLE)
            .upsert(payload, on_conflict="match_id,provider")
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("match_odds upsert failed: %s", e)
        return None


async def get_match_odds(match_id: str, provider: str = "api-football") -> dict | None:
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(MATCH_ODDS_TABLE)
            .select("*")
            .eq("match_id", match_id)
            .eq("provider", provider)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("match_odds query failed: %s", e)
        return None


async def upsert_prediction(row: dict) -> dict | None:
    """predictions에 upsert (match_id, model_version 기준) — 같은 모델 버전
    으로는 앱을 열 때마다 다시 계산하지 않고 저장된 값을 재사용한다."""
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(PREDICTIONS_TABLE)
            .upsert(row, on_conflict="match_id,model_version")
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("predictions upsert failed: %s", e)
        return None


async def get_prediction(match_id: str, model_version: str) -> dict | None:
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(PREDICTIONS_TABLE)
            .select("*")
            .eq("match_id", match_id)
            .eq("model_version", model_version)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("predictions query failed: %s", e)
        return None


async def upsert_model_vs_market(row: dict) -> dict | None:
    try:
        result = await _run_sync(
            lambda: _get_client()
            .table(MODEL_VS_MARKET_TABLE)
            .upsert(row, on_conflict="match_id,model_version,odds_provider")
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("model_vs_market upsert failed: %s", e)
        return None


async def is_duplicate(stadium: str, update_type: str, raw_text: str) -> bool:
    """최근 24시간 내 동일 stadium+type 존재 여부 확인 (하루 1건 제한)."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        result = await _run_sync(
            lambda: _get_client()
            .table(TABLE)
            .select("id")
            .eq("stadium", stadium)
            .eq("type", update_type)
            .gte("collected_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as e:
        logger.error("Supabase duplicate check failed: %s", e)
        return False
