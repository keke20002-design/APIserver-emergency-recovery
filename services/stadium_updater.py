import asyncio
import logging

from services.search_collector import collect_all_stadiums, collect_stadium_updates
from services.text_analyzer import analyze_update
from services.supabase_service import save_pending_update, is_duplicate
from services.raw_store import save_raw, load_raw, clear_raw

logger = logging.getLogger(__name__)


async def _collect_to_raw(stadiums: list[str] | None) -> list[dict]:
    """1단계: 네이버 검색 → 로컬 raw 파일 적재."""
    if stadiums:
        nested = await asyncio.gather(*[collect_stadium_updates(s) for s in stadiums])
        raw = [item for sublist in nested for item in sublist]
    else:
        raw = await collect_all_stadiums()

    save_raw(raw)
    logger.info("[Stage 1] Collected %d raw items → saved to local file", len(raw))
    return raw


def _process_batch(raw_items: list[dict]) -> list[dict]:
    """2단계: HTML 제거 / 키워드 분석 / 신뢰도 계산 / summary 생성."""
    processed = []
    seen: set[str] = set()  # stadium|type → 배치 내 중복 제거 (하루 1건 기준)

    for item in raw_items:
        stadium = item["stadium"]
        update_type = item["type"]
        texts = item.get("texts", [])
        titles = item.get("titles", [])
        links = item.get("links", [])

        result = analyze_update(stadium, update_type, texts, titles=titles, links=links)
        if not result:
            continue

        dedup_key = f"{stadium}|{update_type}"
        if dedup_key in seen:
            logger.debug("[Stage 2] Batch duplicate skipped: %s/%s", stadium, update_type)
            continue
        seen.add(dedup_key)

        processed.append(result)

    logger.info("[Stage 2] Processed %d / %d items", len(processed), len(raw_items))
    return processed


async def _save_to_supabase(processed: list[dict]) -> tuple[int, int]:
    """3단계: Supabase 저장 (24시간 내 중복 제외)."""
    saved = skipped = 0

    for update in processed:
        try:
            if await is_duplicate(update["stadium"], update["type"], update["raw_text"]):
                logger.debug("[Stage 3] DB duplicate skipped: %s/%s", update["stadium"], update["type"])
                skipped += 1
                continue

            result = await save_pending_update(update)
            if result:
                saved += 1
                logger.info(
                    "[Stage 3] Saved: %s / %s — %s",
                    update["stadium"], update["type"], update["message"][:40],
                )
            else:
                skipped += 1
        except Exception as e:
            logger.error("Unexpected error saving %s/%s: %s", update["stadium"], update["type"], e)
            skipped += 1

    logger.info("[Stage 3] Supabase: saved=%d skipped=%d", saved, skipped)
    return saved, skipped


async def run_collection(stadiums: list[str] | None = None) -> dict:
    """
    야구장 업데이트 수집 메인 함수 (3단계 파이프라인).

    Stage 1   — 네이버 검색 → 로컬 raw 파일
    Stage 2   — HTML 제거 / 키워드 / 신뢰도 / summary 생성
    Stage 3   — Supabase stadium_updates 저장 (pending)
    """
    logger.info("=== Stadium update collection started (stadiums=%s) ===", stadiums or "all")

    raw_items = await _collect_to_raw(stadiums)
    processed = _process_batch(raw_items)
    # Stage 2.5 (Gemini AI 요약) 비활성화
    saved, skipped = await _save_to_supabase(processed)

    clear_raw()

    result = {"saved": saved, "skipped": skipped, "raw_collected": len(raw_items)}
    logger.info("=== Collection complete: %s ===", result)
    return result
