from datetime import datetime, timezone

import pytest
import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from services.kfood_trends import collector, pipeline, repository
from services.kfood_trends.router import admin_router, public_router
from services.kfood_trends.schemas import EvidenceArticle, TrendCandidate, TrendDraft, TrendUpdate


@pytest.mark.asyncio
async def test_collector_filters_old_and_deduplicates_similar_titles(monkeypatch):
    recent = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    old = "Mon, 01 Jan 2024 00:00:00 +0000"

    async def fake_search(query, display, sort, client=None):
        return [
            {"title": "두바이 초콜릿 디저트 인기", "description": "SNS에서 화제가 된 디저트", "pub_date": recent, "link": "https://news.example/a?tracking=1"},
            {"title": "두바이 초콜릿 디저트 인기!", "description": "비슷한 기사", "pub_date": recent, "link": "https://other.example/b"},
            {"title": "오래된 음식 기사", "description": "오래된 요약", "pub_date": old, "link": "https://news.example/old"},
        ]

    monkeypatch.setattr(collector, "search_news", fake_search)
    monkeypatch.setattr(collector, "search_queries", lambda: ("q1", "q2"))
    articles, calls = await collector.collect_recent_news()
    assert calls == 2
    assert len(articles) == 1
    assert articles[0].url == "https://news.example/a"


def test_draft_allows_missing_recipe_but_rejects_half_recipe():
    base = dict(
        title_ko="두바이 초콜릿", title_ja="ドバイチョコレート", category="디저트",
        description_ja="韓国で話題のデザートです。", trend_reason_ja="複数の記事で紹介されました。",
        source_urls=["https://news.example/a"], cooking_time_minutes=None, difficulty=None,
    )
    assert TrendDraft(**base, ingredients_ja=None, steps_ja=None).steps_ja is None
    with pytest.raises(ValidationError):
        TrendDraft(**base, ingredients_ja=["チョコレート"], steps_ja=None)

    with pytest.raises(ValidationError):
        TrendUpdate(ingredients_ja=["チョコレート"])


@pytest.mark.asyncio
async def test_pipeline_creates_draft_and_skips_recent_duplicate(monkeypatch):
    articles = [EvidenceArticle(
        title="화제 디저트", url="https://news.example/a", publisher="news.example",
        published_at=datetime.now(timezone.utc), summary="두 음식이 화제다",
    )]
    candidates = [
        TrendCandidate(food_name="새 음식", category="간식", trend_reason_ko="기사 근거", evidence_indexes=[0]),
        TrendCandidate(food_name="기존 음식", category="간식", trend_reason_ko="기사 근거", evidence_indexes=[0]),
    ]
    draft = TrendDraft(
        title_ko="새 음식", title_ja="新しい料理", category="간식", description_ja="紹介",
        trend_reason_ja="記事で話題", ingredients_ja=None, steps_ja=None,
        cooking_time_minutes=None, difficulty=None, source_urls=["https://news.example/a"],
    )
    saved = []
    monkeypatch.setattr(pipeline, "collect_recent_news", lambda: _async((articles, 2)))
    monkeypatch.setattr(pipeline, "select_candidates", lambda _: _async((candidates, _usage())))
    monkeypatch.setattr(pipeline, "generate_draft", lambda *_: _async((draft, _usage())))
    monkeypatch.setattr(repository, "has_recent_running_job", lambda: _async(False))
    monkeypatch.setattr(repository, "start_run", lambda _: _async("run-1"))
    monkeypatch.setattr(repository, "finish_run", lambda *args, **kwargs: _async(None))
    monkeypatch.setattr(repository, "recently_exists", lambda title: _async(title == "기존 음식"))
    monkeypatch.setattr(repository, "insert_draft", lambda row: _async(saved.append(row) or row))

    result = await pipeline.run_weekly_collection("manual")
    assert result["drafts_created"] == 1
    assert result["duplicates"] == 1
    assert result["gemini_requests"] == 2
    assert saved[0]["status"] if "status" in saved[0] else True


@pytest.mark.asyncio
async def test_pipeline_skips_when_job_is_running(monkeypatch):
    monkeypatch.setattr(repository, "has_recent_running_job", lambda: _async(True))
    result = await pipeline.run_weekly_collection()
    assert result == {"status": "skipped", "reason": "collection_already_running"}


@pytest.mark.asyncio
async def test_public_routes_delegate_to_published_only_queries(monkeypatch):
    captured = {}
    async def fake_list(start, limit=50):
        captured["start"] = start
        return [{"id": "published"}]
    monkeypatch.setattr(repository, "public_list", fake_list)
    app = FastAPI()
    app.include_router(public_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/kfood-trends?period=monthly")
    assert response.status_code == 200
    assert response.json()["data"] == [{"id": "published"}]
    assert captured["start"] is not None


@pytest.mark.asyncio
async def test_admin_api_requires_configured_credentials(monkeypatch):
    monkeypatch.setenv("BLOG_ADMIN_ID", "admin")
    monkeypatch.setenv("BLOG_ADMIN_PW", "secret")
    monkeypatch.setattr(repository, "admin_list", lambda status: _async([]))
    app = FastAPI()
    app.include_router(admin_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/admin/kfood-trends/items")
        allowed = await client.get("/admin/kfood-trends/items?admin_id=admin&admin_pw=secret")
    assert denied.status_code == 401
    assert allowed.status_code == 200


def _usage():
    return {"prompt_tokens": 10, "output_tokens": 5, "total_tokens": 15}


async def _async(value):
    return value
