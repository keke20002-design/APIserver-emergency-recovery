"""
KBO 야구 커뮤니티 인기글 수집 (DCinside 야갤 / MLB파크 / 에펨코리아)
제목 + 베스트 댓글 조합으로 Gemini 요약 입력용 텍스트 구성
"""
import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.google.com/",
}


async def _get(url: str, client: httpx.AsyncClient) -> BeautifulSoup | None:
    try:
        r = await client.get(url, headers=_HEADERS, timeout=12.0, follow_redirects=True)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        logger.warning("community fetch failed [%s]: %s", url, e)
        return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ── DCinside 통합야구갤러리 ───────────────────────────────────────────────
async def _crawl_dcinside(client: httpx.AsyncClient, max_items: int = 15) -> list[str]:
    url = "https://gall.dcinside.com/board/lists/?id=baseball_new9&sort_type=N&exception_mode=recommand"
    soup = await _get(url, client)
    if not soup:
        return []

    items: list[str] = []
    for row in soup.select("tr.ub-content.us-post, tr.ub-content")[:max_items]:
        title_el = row.select_one("td.gall_tit a:not(.reply_numbox)")
        if not title_el:
            continue
        title = _clean(title_el.get_text())
        # 댓글 수 있으면 추가
        reply_el = row.select_one(".reply_numbox .reply_num, em.num_reply")
        comment_hint = f" [댓글{reply_el.get_text(strip=True)}]" if reply_el else ""
        if title:
            items.append(title + comment_hint)
    logger.info("DCinside: %d items", len(items))
    return items


# ── MLB파크 ──────────────────────────────────────────────────────────────
async def _crawl_mlbpark(client: httpx.AsyncClient, max_items: int = 15) -> list[str]:
    url = "https://mlbpark.donga.com/mp/b.php?b=bullpen&order=recom"
    soup = await _get(url, client)
    if not soup:
        return []

    items: list[str] = []
    seen: set[str] = set()
    for el in soup.select(".title a, a.title")[:max_items * 3]:
        title = _clean(el.get_text())
        if title and len(title) > 5 and title not in seen:
            seen.add(title)
            items.append(title)
        if len(items) >= max_items:
            break
    logger.info("MLB파크: %d items", len(items))
    return items


# ── 에펨코리아 야구 ───────────────────────────────────────────────────────
async def _crawl_fmkorea(client: httpx.AsyncClient, max_items: int = 15) -> list[str]:
    url = "https://www.fmkorea.com/index.php?mid=baseball&sort_index=recentness&order_type=desc"
    soup = await _get(url, client)
    if not soup:
        return []

    items: list[str] = []
    for el in soup.select("h3.title a, .li h3 a, li.li h4 a, td.title a")[:max_items]:
        title = _clean(el.get_text())
        if title and len(title) > 3:
            items.append(title)
    logger.info("에펨코리아: %d items", len(items))
    return items


# ── 통합 수집 ─────────────────────────────────────────────────────────────
async def collect_community_posts(max_per_site: int = 15) -> list[str]:
    """세 커뮤니티 인기글 제목을 합산해 반환. 실패 사이트는 건너뜀."""
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        dc, mlb, fm = await asyncio.gather(
            _crawl_dcinside(client, max_per_site),
            _crawl_mlbpark(client, max_per_site),
            _crawl_fmkorea(client, max_per_site),
            return_exceptions=True,
        )

    all_posts: list[str] = []
    for result in (dc, mlb, fm):
        if isinstance(result, list):
            all_posts.extend(result)
        else:
            logger.warning("community crawler error: %s", result)

    logger.info("community posts collected: %d total", len(all_posts))
    return all_posts
