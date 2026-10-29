"""TrendLab Global — EN AI/Developer 토픽용 실시간 신호 수집기.

app/trendlab/(한국어, Naver 전용)과 완전히 분리된 별도 컬렉터 — 서로 참조하지 않는다.
WordPress EN 허브 파이프라인(services/blog_topic_engine.py::pick_topic_en_hub)의
"트렌딩 30%" 슬라이스에만 데이터를 공급한다. 미리보기 UI/수동 생성 화면은 없음 —
EN 파이프라인은 이미 완전자동이라 필요 없음.
"""
import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TopicItem:
    title: str
    url: str = ""
    source: str = ""
    weight: float = 1.0
    rank_score: float = 0.0  # 소스 내부 랭킹 기반 0~100 점수 (weight 곱하기 전)

    @property
    def score(self) -> float:
        return self.rank_score * self.weight


class TopicProvider(ABC):
    name: str = "base"
    weight: float = 1.0

    @abstractmethod
    async def fetch(self) -> list[TopicItem]: ...


def _rank_scored(titles_urls: list[tuple[str, str]], source: str, weight: float) -> list[TopicItem]:
    """이미 '베스트 먼저' 순서인 리스트를 등수 기반 0~100 점수로 변환.
    소스마다 지표가 이질적이라(별 개수 vs 투표 수 vs 조회수) 절대값 정규화 대신
    각 소스 내부 순위만 신뢰하고, 소스 간 비교는 weight로 가중한다."""
    n = len(titles_urls)
    out = []
    for i, (title, url) in enumerate(titles_urls):
        score = max(10.0, 100.0 - i * (90.0 / max(n, 1)))
        out.append(TopicItem(title=title, url=url, source=source, weight=weight, rank_score=score))
    return out


# ── GitHub Trending (weight 1.4) — 공식 API 없음, HTML 스크래핑 ──────────────

_GH_REPO_RE = re.compile(
    r'<h2[^>]*class="h3 lh-condensed"[^>]*>\s*<a[^>]*href="/([^/"]+/[^/"]+)"',
    re.DOTALL,
)


class GitHubTrendingProvider(TopicProvider):
    name = "github_trending"
    weight = 1.4

    async def fetch(self) -> list[TopicItem]:
        try:
            async with httpx.AsyncClient(
                timeout=10, headers={"User-Agent": "Mozilla/5.0 (trendlab-global/1.0)"},
            ) as client:
                r = await client.get("https://github.com/trending", params={"since": "daily"})
                r.raise_for_status()
                html = r.text
            repos = _GH_REPO_RE.findall(html)
            titles_urls = [
                (repo.replace("\n", "").strip(), f"https://github.com/{repo.strip()}")
                for repo in repos[:30]
            ]
            return _rank_scored(titles_urls, self.name, self.weight)
        except Exception as e:
            logger.warning("GitHub Trending fetch failed: %s", e)
            return []


# ── Product Hunt (weight 1.4) — 토큰 없으면 조용히 스킵 ──────────────────────

class ProductHuntProvider(TopicProvider):
    name = "producthunt"
    weight = 1.4

    async def fetch(self) -> list[TopicItem]:
        token = os.getenv("PRODUCTHUNT_API_TOKEN", "")
        if not token:
            logger.debug("Product Hunt skipped — no PRODUCTHUNT_API_TOKEN set")
            return []
        query = """
        query { posts(order: VOTES, first: 20) {
            edges { node { name tagline url votesCount } }
        } }
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    "https://api.producthunt.com/v2/api/graphql",
                    json={"query": query},
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                data = r.json()
            edges = data.get("data", {}).get("posts", {}).get("edges", [])
            titles_urls = [
                (f"{e['node']['name']} — {e['node']['tagline']}"[:150], e["node"].get("url", ""))
                for e in edges
            ]
            return _rank_scored(titles_urls, self.name, self.weight)
        except Exception as e:
            logger.warning("Product Hunt fetch failed: %s", e)
            return []


# ── Hacker News (weight 1.2) — Top/Show/Ask Stories ──────────────────────────

class HackerNewsProvider(TopicProvider):
    name = "hackernews"
    weight = 1.2

    async def fetch(self) -> list[TopicItem]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                lists = await asyncio.gather(
                    client.get("https://hacker-news.firebaseio.com/v0/topstories.json"),
                    client.get("https://hacker-news.firebaseio.com/v0/showstories.json"),
                    client.get("https://hacker-news.firebaseio.com/v0/askstories.json"),
                    return_exceptions=True,
                )
                ids: list[int] = []
                for r in lists:
                    if not isinstance(r, Exception):
                        ids.extend(r.json()[:20])
                ids = list(dict.fromkeys(ids))[:60]
                tasks = [client.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json") for i in ids]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            titles_urls = []
            for res in results:
                if isinstance(res, Exception):
                    continue
                try:
                    item = res.json()
                    t = item.get("title", "")
                    if t and len(t) > 5:
                        url = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id')}"
                        titles_urls.append((t[:150], url))
                except Exception:
                    pass
            return _rank_scored(titles_urls, self.name, self.weight)
        except Exception as e:
            logger.warning("Hacker News fetch failed: %s", e)
            return []


# ── Reddit (weight 1.2) — 우선순위 서브레딧 ──────────────────────────────────

_REDDIT_SUBS = "ChatGPT+ClaudeAI+OpenAI+artificial+MachineLearning+programming+webdev+Python+LocalLLaMA"


class RedditProvider(TopicProvider):
    name = "reddit"
    weight = 1.2

    async def fetch(self) -> list[TopicItem]:
        try:
            async with httpx.AsyncClient(
                timeout=8, headers={"User-Agent": "trendlab-global/1.0"},
            ) as client:
                r = await client.get(
                    f"https://www.reddit.com/r/{_REDDIT_SUBS}/hot.json", params={"limit": 40},
                )
                r.raise_for_status()
                posts = r.json()["data"]["children"]
            titles_urls = [
                (p["data"]["title"][:150], f"https://reddit.com{p['data'].get('permalink', '')}")
                for p in posts if not p["data"].get("stickied")
            ][:30]
            return _rank_scored(titles_urls, self.name, self.weight)
        except Exception as e:
            logger.warning("Reddit fetch failed: %s", e)
            return []


# ── Google News RSS (weight 0.8) — 허브 키워드 기반, 키 불필요 ────────────────

_NEWS_QUERIES = [
    "ChatGPT", "Claude AI", "Claude Code", "Cursor", "Gemini", "OpenAI", "Anthropic",
    "OpenRouter", "Docker", "Kubernetes", "GitHub Copilot", "n8n", "MCP",
    "AI Agents", "Automation",
]


class NewsProvider(TopicProvider):
    name = "news"
    weight = 0.8

    async def _fetch_one(self, client: httpx.AsyncClient, query: str) -> list[tuple[str, str]]:
        try:
            r = await client.get(
                "https://news.google.com/rss/search",
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
            r.raise_for_status()
            root = ET.fromstring(r.text)
            out = []
            for item in root.findall(".//item")[:3]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if title:
                    out.append((title[:150], link))
            return out
        except Exception as e:
            logger.debug("News RSS failed for '%s': %s", query, e)
            return []

    async def fetch(self) -> list[TopicItem]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                results = await asyncio.gather(
                    *(self._fetch_one(client, q) for q in _NEWS_QUERIES), return_exceptions=True,
                )
            titles_urls: list[tuple[str, str]] = []
            for r in results:
                if isinstance(r, list):
                    titles_urls.extend(r)
            return _rank_scored(titles_urls, self.name, self.weight)
        except Exception as e:
            logger.warning("News provider failed: %s", e)
            return []


# ── YouTube US Tech Trending (weight 1.0) — 기존 YOUTUBE_API_KEY 재사용 ───────

class YouTubeProvider(TopicProvider):
    name = "youtube"
    weight = 1.0

    async def fetch(self) -> list[TopicItem]:
        api_key = os.getenv("YOUTUBE_API_KEY", "")
        if not api_key:
            logger.debug("YouTube provider skipped — no YOUTUBE_API_KEY set")
            return []
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "snippet",
                        "chart": "mostPopular",
                        "regionCode": "US",
                        "videoCategoryId": "28",  # Science & Technology
                        "maxResults": 30,
                        "key": api_key,
                    },
                )
                r.raise_for_status()
                data = r.json()
            titles_urls = [
                (i["snippet"]["title"][:150], f"https://www.youtube.com/watch?v={i['id']}")
                for i in data.get("items", [])
            ]
            return _rank_scored(titles_urls, self.name, self.weight)
        except Exception as e:
            logger.warning("YouTube provider failed: %s", e)
            return []


# ── 통합 수집 ─────────────────────────────────────────────────────────────

async def collect_all_weighted() -> list[TopicItem]:
    """6개 Provider 동시 수집 — 하나가 실패해도 나머지는 정상 진행. score 내림차순 정렬."""
    providers: list[TopicProvider] = [
        GitHubTrendingProvider(), ProductHuntProvider(), HackerNewsProvider(),
        RedditProvider(), NewsProvider(), YouTubeProvider(),
    ]
    results = await asyncio.gather(*(p.fetch() for p in providers), return_exceptions=True)
    items: list[TopicItem] = []
    for p, r in zip(providers, results):
        if isinstance(r, Exception):
            logger.warning("TrendLab Global provider '%s' failed: %s", p.name, r)
            continue
        items.extend(r)
    items.sort(key=lambda it: it.score, reverse=True)
    logger.info(
        "TrendLab Global collected: %d items (%s)",
        len(items),
        ", ".join(f"{p.name}={sum(1 for i in items if i.source == p.name)}" for p in providers),
    )
    return items
