"""blog_db — 하위 호환 래퍼. 실제 로직은 storage.PostRepository에 있음."""
from storage.post_repository import PostRepository, Status

_repo = PostRepository()


# ── 공개 API (기존 코드와 시그니처 동일) ────────────────────────────────────

def init_db() -> None:
    _repo.init_db()


def is_keyword_used(keyword: str, months: int = 6) -> bool:
    return _repo.is_keyword_used(keyword, months)


def mark_keyword_used(keyword: str, topic: str) -> None:
    _repo.mark_keyword_used(keyword, topic)


def get_used_keywords(limit: int = 100) -> list[dict]:
    return _repo.get_used_keywords(limit)


def save_post_log(
    keyword: str,
    topic: str,
    title: str,
    wp_post_id: int | None = None,
    wp_url: str | None = None,
    status: str = Status.READY,
    error_msg: str | None = None,
    platform: str = "wordpress",
    language: str = "ko",
    thumbnail_url: str | None = None,
    meta_description: str | None = None,
    source: str | None = None,
    intent: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    summary: list[str] | None = None,
    traffic_score: int | None = None,
    competition: int | None = None,
    trend_level: str | None = None,
    score_reason: list[str] | None = None,
    ctr_score: int | None = None,
    seo_score: int | None = None,
    ai_risk: str | None = None,
    dopamine_score: int | None = None,
    hub: str | None = None,
    subtopic_key: str | None = None,
) -> int:
    return _repo.save_post(
        keyword=keyword, topic=topic, title=title,
        wp_post_id=wp_post_id, wp_url=wp_url,
        status=status, error_msg=error_msg,
        platform=platform, language=language,
        thumbnail_url=thumbnail_url, meta_description=meta_description,
        source=source, intent=intent,
        content=content, tags=tags, summary=summary,
        traffic_score=traffic_score, competition=competition,
        trend_level=trend_level, score_reason=score_reason,
        ctr_score=ctr_score, seo_score=seo_score,
        ai_risk=ai_risk, dopamine_score=dopamine_score,
        hub=hub, subtopic_key=subtopic_key,
    )


def get_recent_posts(limit: int = 20) -> list[dict]:
    return _repo.get_posts(limit)


def get_recent_keyword_set(days: int = 7) -> set[str]:
    return _repo.get_recent_keyword_set(days)


def get_published_keywords_for_hash(doc_hash: str) -> set[str]:
    return _repo.get_published_keywords_for_hash(doc_hash)


def save_published_document(doc_hash: str, keyword: str, wp_post_id: int, wp_url: str) -> None:
    _repo.save_published_document(doc_hash, keyword, wp_post_id, wp_url)


def increment_view_count(wp_post_id: int) -> int:
    return _repo.increment_view_count(wp_post_id)


def get_related_posts(
    keyword: str,
    category: str,
    exclude_wp_id: int | None = None,
    limit: int = 3,
) -> list[dict]:
    return _repo.get_related_posts(keyword, category, exclude_wp_id, limit)


def get_diversity_data(
    days: int = 60,
    platform: str | None = None,
    language: str | None = None,
    limit: int = 200,
) -> dict:
    return _repo.get_diversity_data(days, platform=platform, language=language, limit=limit)


def get_hub_used_subtopics(hub: str) -> set[str]:
    return _repo.get_hub_used_subtopics(hub)


def get_hub_post_counts(language: str = "en") -> dict[str, int]:
    return _repo.get_hub_post_counts(language)


def get_hub_related_posts(hub: str, exclude_wp_id: int | None = None, limit: int = 3) -> list[dict]:
    return _repo.get_hub_related_posts(hub, exclude_wp_id, limit)


def get_last_post_hub(language: str = "en") -> str | None:
    return _repo.get_last_post_hub(language)


def get_last_post_intent(language: str = "en") -> str | None:
    return _repo.get_last_post_intent(language)


def get_category_related_posts(category_topic: str, exclude_wp_id: int | None = None, limit: int = 5) -> list[dict]:
    return _repo.get_category_related_posts(category_topic, exclude_wp_id, limit)


def get_stats() -> dict:
    return _repo.get_stats()
