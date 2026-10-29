"""TrendLab Service — 분석(analyze) + 생성(generate) 분리."""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DRAFTS_DIR = Path(__file__).parent.parent.parent / "drafts"
_TOP10_CACHE_FILE = DRAFTS_DIR / "trendlab_top10_cache.json"
_TOP10_CACHE_MAX_AGE = 3 * 60 * 60  # 3시간 — 이보다 오래된 디스크 캐시는 이미 topic이 지나갔을 가능성이 높아 무시

# 마지막 analyze() 결과 캐시 — generate()가 근거 기사 제목(example_title)을 재사용하기 위함.
# (재검색이 원 출처 기사를 다시 못 찾아서 실제 존재하는 사실을 "확인 안 됨"으로 잘못 단정하는 것을 방지)
# 개발 중 서버가 analyze() 직후 재시작되면 이 메모리 캐시가 비워져 generate()가 근거 0건으로
# 넘어가는 버그가 있었다 — 디스크에도 저장해 재시작에도 살아남게 한다.
_last_top10: list[dict] = []


def _save_top10_cache(top10: list[dict]) -> None:
    try:
        DRAFTS_DIR.mkdir(exist_ok=True)
        _TOP10_CACHE_FILE.write_text(json.dumps(top10, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("TrendLab top10 cache save failed: %s", e)


def _load_top10_cache() -> list[dict]:
    try:
        if not _TOP10_CACHE_FILE.exists():
            return []
        age = datetime.now().timestamp() - _TOP10_CACHE_FILE.stat().st_mtime
        if age > _TOP10_CACHE_MAX_AGE:
            return []
        return json.loads(_TOP10_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("TrendLab top10 cache load failed: %s", e)
        return []


def _html_to_md(html: str) -> str:
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", html, flags=re.DOTALL)
    text = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<li>(.*?)</li>", r"- \1", text, flags=re.DOTALL)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n", text, flags=re.DOTALL)
    text = re.sub(r"<mark>(.*?)</mark>", r"**\1**", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def run_trendlab_analyze() -> dict:
    """
    수집 + 분석만 실행 → Blog TOP10 반환. 포스트 생성 없음.

    top10은 "지금 뜨고 있는 것"(Trend)이 아니라 "블로그로 쓸 가치가 있는 것"(Blog)이다 —
    Trend 후보가 있었어도 관련 source 부족 등으로 전부 제외되면 top10은 빈 리스트가 될 수
    있고, 이는 오류가 아니라 정상적인 결과다("관련 정보가 없다"도 정상 상태). Trend 후보
    자체가 하나도 없었을 때만(수집 실패 등) 에러를 낸다.

    반환: {"top10": [...], "trend_debug": [...], "collected_count": int}
    """
    from app.trendlab.collector import collect_all
    from app.trendlab.analyzer import extract_topics, get_last_trend_debug

    logger.info("TrendLab: collecting...")
    items = await collect_all()
    logger.info("TrendLab collected: %d items", len(items))

    logger.info("TrendLab: analyzing...")
    top10 = await extract_topics(items)
    trend_debug = get_last_trend_debug()
    if not top10 and not trend_debug:
        raise RuntimeError("TOP10 추출 실패 — 수집된 데이터가 충분하지 않습니다")

    global _last_top10
    _last_top10 = top10
    _save_top10_cache(top10)

    if top10:
        logger.info("TrendLab Blog TOP10: %s", [t["topic"] for t in top10])
    else:
        logger.info(
            "TrendLab: trend 후보 %d개 중 blog-eligible 0개 (관련 source 부족 등으로 전부 제외)",
            len(trend_debug),
        )

    return {
        "top10": _strip_top10_for_response(top10),
        "trend_debug": trend_debug,
        "collected_count": len(items),
    }


def _strip_top10_for_response(top10: list[dict]) -> list[dict]:
    """관리자 화면에 보낼 TOP10 — source_items(원문 전체)는 응답 크기만 키우므로 제외.
    generate()가 쓰는 _last_top10 캐시에는 그대로 유지된다."""
    return [{k: v for k, v in t.items() if k != "source_items"} for t in top10]


async def run_trendlab_generate(topic: str, category: str) -> dict:
    """
    특정 토픽으로 포스트 생성 → drafts + DB READY 저장.
    반환: {"title", "keyword", "category", "db_id", "thumbnail_url", "files"}
    """
    from app.trendlab.analyzer import is_media_outlet
    from app.trendlab.generator import generate_trendlab_post
    from services.blog_gemini_service import generate_post_image
    from services.blog_pipeline import upload_image

    if is_media_outlet(topic):
        raise ValueError(f"'{topic}'은 언론사/방송사명 — 포스트 생성 불가")

    global _last_top10
    if not _last_top10:
        _last_top10 = _load_top10_cache()
        if _last_top10:
            logger.info(
                "TrendLab: in-memory top10 cache empty (서버 재시작 등) — 디스크 캐시로 복구 (%d topics)",
                len(_last_top10),
            )

    logger.info("TrendLab: generating post for '%s' (%s)", topic, category)
    post = await generate_trendlab_post(topic, _last_top10, category=category)

    # 썸네일
    thumbnail_url: str | None = None
    try:
        img = await generate_post_image(
            post["keyword"], post["title"], post["category"], summary=post.get("summary")
        )
        if img:
            now_str = datetime.now().strftime("%Y%m%d%H%M%S")
            upload = await upload_image(img[0], img[1], f"trendlab-{now_str}.jpg")
            thumbnail_url = upload.get("url")
    except Exception as e:
        logger.warning("TrendLab thumbnail failed: %s", e)

    # drafts 저장
    DRAFTS_DIR.mkdir(exist_ok=True)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    json_data = {
        "generated_at": now_str,
        "selected_topic": topic,
        "keyword": post["keyword"],
        "category": post["category"],
        "title": post["title"],
        "meta_description": post["meta_description"],
        "tags": post["tags"],
        "summary": post.get("summary"),
        "thumbnail_url": thumbnail_url,
        "content": post["content"],
    }

    json_path = DRAFTS_DIR / "trend_test.json"
    html_path = DRAFTS_DIR / "trend_test.html"
    md_path   = DRAFTS_DIR / "trend_test.md"

    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(
        f'<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">'
        f'<title>{post["title"]}</title></head><body>'
        f'<h1>{post["title"]}</h1>'
        f'<p><strong>키워드:</strong> {post["keyword"]} | <strong>카테고리:</strong> {post["category"]}</p>'
        f'<p><strong>생성일시:</strong> {now_str}</p><hr>{post["content"]}</body></html>',
        encoding="utf-8",
    )
    md_content = f"# {post['title']}\n\n> 키워드: {post['keyword']} | 카테고리: {post['category']}\n\n"
    md_content += f"**생성:** {now_str}\n\n---\n\n" + _html_to_md(post["content"])
    md_path.write_text(md_content, encoding="utf-8")

    logger.info("TrendLab drafts saved: %s", DRAFTS_DIR)

    # DB READY 저장
    db_id: int | None = None
    try:
        from services.blog_db import save_post_log
        db_id = save_post_log(
            keyword=post["keyword"],
            topic=post["category"],
            title=post["title"],
            status="READY",
            platform="naver",
            language="ko",
            thumbnail_url=thumbnail_url,
            meta_description=post["meta_description"],
            content=post["content"],
            tags=post["tags"],
            summary=post.get("summary"),
            source="trendlab",
        )
        logger.info("TrendLab DB saved: id=%s", db_id)
    except Exception as e:
        logger.warning("TrendLab DB save failed: %s", e)

    return {
        "title": post["title"],
        "keyword": post["keyword"],
        "category": post["category"],
        "db_id": db_id,
        "thumbnail_url": thumbnail_url,
        "files": {
            "json": str(json_path),
            "html": str(html_path),
            "md": str(md_path),
        },
    }
