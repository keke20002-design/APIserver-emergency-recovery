"""TrendLab Generator — TOP1 토픽으로 이슈 분석 블로그 글 생성.

실제 생성 로직(사실/주장 분리 → 이슈 분석 기사 생성)은 issue_generator.py에 있다.
이 모듈은 top10에서 해당 토픽의 원문 클러스터(source_items)를 꺼내 넘겨주는 역할만 한다.
"""
import logging

logger = logging.getLogger(__name__)


async def generate_trendlab_post(topic: str, top10: list[dict], category: str = "") -> dict:
    """
    topic: 이슈구문 (예: "리센느 컴백")
    top10: 전체 순위 목록 (analyzer가 사건별로 묶은 source_items 포함)
    category: 분석기에서 전달받은 카테고리 (없으면 자체 추론)
    반환: {title, content, tags, summary, meta_description, keyword}
    """
    from app.trendlab.issue_generator import generate_issue_post

    if not category or category == "기타":
        category = _infer_category(topic)

    # 분석 단계(동일 사건 묶기)에서 이 토픽에 매칭된 원문 목록 + 대표 기사 제목
    anchor_title = ""
    source_items: list[dict] = []
    for t in top10:
        if t.get("topic") == topic:
            anchor_title = t.get("example_title", "") or ""
            source_items = list(t.get("source_items") or [])
            break

    # 재검색이 원 출처 기사를 놓쳐도 근거로 강제 포함
    if anchor_title and not any(i.get("title", "").strip() == anchor_title.strip() for i in source_items):
        source_items = [{
            "source": "naver", "title": anchor_title, "description": "",
            "published_at": None, "publisher": "", "url": "",
        }] + source_items

    logger.info(
        "TrendLab generating: topic='%s' category=%s sources=%d anchor=%r",
        topic, category, len(source_items), anchor_title,
    )

    from app.trendlab.source_enricher import prepare_sources
    source_items = await prepare_sources(topic, source_items)
    result = await generate_issue_post(topic, category, source_items, anchor_title=anchor_title)

    return {
        "keyword": topic,
        "topic": topic,
        "category": category,
        "title": result["title"],
        "content": result["content"],
        "tags": result["tags"],
        "summary": result["summary"],
        "meta_description": result["meta_description"],
        "top10": top10,
    }


def _infer_category(topic: str) -> str:
    """토픽명에서 네이버 카테고리 추론."""
    _SPORTS_HINTS = {"FC", "감독", "선수", "코치", "리그", "KBO", "EPL", "K리그",
                     "올림픽", "월드컵", "골프", "야구", "축구", "농구", "배구"}
    _IT_HINTS = {"AI", "챗GPT", "ChatGPT", "GPT", "애플", "삼성", "구글",
                 "마이크로소프트", "메타", "엔비디아", "반도체", "스마트폰"}
    _STOCK_HINTS = {"주식", "코인", "비트코인", "ETF", "증시", "펀드", "투자"}
    _CAR_HINTS = {"자동차", "전기차", "테슬라", "현대", "기아", "BMW", "벤츠"}

    t = topic
    if any(h in t for h in _SPORTS_HINTS):
        return "스포츠"
    if any(h in t for h in _IT_HINTS):
        return "IT"
    if any(h in t for h in _STOCK_HINTS):
        return "주식"
    if any(h in t for h in _CAR_HINTS):
        return "자동차"
    # 기본값: 연예 (인물명 대부분)
    return "연예"
