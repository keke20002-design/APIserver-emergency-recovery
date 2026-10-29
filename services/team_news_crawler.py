"""
각 KBO 구단 홈페이지에서 당일 공지/이벤트를 수집합니다.
- 서버사이드 렌더링 (httpx+BS4): 키움, 롯데
- JS 렌더링 (Playwright+BS4): LG, 두산, SSG, 한화, NC, KT, KIA
"""
import asyncio
import concurrent.futures
import logging
import re
from datetime import date, timedelta

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_DATE_PAT = re.compile(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})")


def _today() -> str:
    return date.today().isoformat()


def _norm_date(raw: str) -> str:
    m = _DATE_PAT.search(raw.strip() if raw else "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _is_today(raw: str) -> bool:
    nd = _norm_date(raw)
    return nd == _today() if nd else False


def _is_recent(raw: str, days: int = 7) -> bool:
    nd = _norm_date(raw)
    if not nd:
        return False
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return nd >= cutoff


def _item(team: str, ctype: str, title: str, raw_date: str, url: str) -> dict:
    return {
        "team": team,
        "type": ctype,
        "title": title.strip(),
        "published_date": _norm_date(raw_date),
        "url": url,
        "summary": None,
    }


async def _get_bs4(url: str, client: httpx.AsyncClient) -> BeautifulSoup | None:
    try:
        r = await client.get(url, headers=_HEADERS, timeout=15.0, follow_redirects=True)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        logger.error("httpx fetch failed [%s]: %s", url, e)
        return None


# ──────────────────────────────
# 키움히어로즈 (BS4)
# ──────────────────────────────
async def _crawl_kiwoom(client: httpx.AsyncClient) -> list[dict]:
    results = []
    base = "https://heroesbaseball.co.kr"
    soup = await _get_bs4(f"{base}/story/heroesNews/list.do", client)
    if not soup:
        return results
    for item in soup.select(".headNotice ul li, ul.teamNews li"):
        a = item.select_one("h4 a[href], a[href]")
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        span = item.select_one("span")
        raw_d = span.get_text(strip=True) if span else ""
        if not _is_today(raw_d):
            continue
        href = a["href"]
        if not href.startswith("http"):
            href = f"{base}/story/heroesNews/{href.lstrip('/')}"
        results.append(_item("키움", "notice", title, raw_d, href))
    return results


# ──────────────────────────────
# 롯데자이언츠 (BS4)
# ──────────────────────────────
async def _crawl_lotte(client: httpx.AsyncClient) -> list[dict]:
    results = []
    base = "https://www.giantsclub.com"
    soup = await _get_bs4(f"{base}/html/?pcode=783", client)
    if not soup:
        return results
    for item in soup.select("div.news-list-item"):
        a = item.select_one("div.news-list-cont a[href]")
        if not a:
            continue
        title_el = a.select_one("strong")
        title = (title_el or a).get_text(strip=True)
        title = re.sub(r"\s*\[?새글\]?\s*", "", title).strip()
        if not title:
            continue
        day_el = item.select_one(".news-list-date-day")
        month_el = item.select_one(".news-list-date-month")
        if day_el and month_el:
            raw_d = f"{month_el.get_text(strip=True)}.{day_el.get_text(strip=True).zfill(2)}"
        else:
            raw_d = ""
        if not _is_today(raw_d):
            continue
        href = a["href"]
        url = href if href.startswith("http") else f"{base}/html/{href.lstrip('/')}"
        cat_el = item.select_one(".new_col02, .news-list-type p")
        ctype = "event" if cat_el and "이벤트" in cat_el.get_text() else "notice"
        results.append(_item("롯데", ctype, title, raw_d, url))
    return results


# ──────────────────────────────
# LG트윈스 (Playwright)
# ──────────────────────────────
async def _crawl_lg(browser: Browser) -> list[dict]:
    results = []
    base = "https://www.lgtwins.com"
    for ctype, path in [("notice", "/twins/feed/notices"), ("event", "/twins/feed/events")]:
        soup = await _get_playwright(f"{base}{path}", browser)
        if not soup:
            continue
        sel = "notice/detail" if ctype == "notice" else "event/detail"
        for a in soup.select(f'a[href*="{sel}"]'):
            title_el = a.select_one("span.title")
            date_el = a.select_one("span.date")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            raw_d = date_el.get_text(strip=True) if date_el else ""
            if not title or not _is_today(raw_d):
                continue
            href = a["href"]
            href = re.sub(r";jsessionid=[^?]+", "", href)
            url = href if href.startswith("http") else f"{base}{href}"
            results.append(_item("LG", ctype, title, raw_d, url))
    return results


# ──────────────────────────────
# 두산베어스 (Playwright)
# ──────────────────────────────
async def _crawl_doosan(browser: Browser) -> list[dict]:
    results = []
    base = "https://www.doosanbears.com"
    for ctype, path in [("notice", "/bears/notice/list"), ("event", "/bears/event/list")]:
        soup = await _get_playwright(f"{base}{path}", browser)
        if not soup:
            continue
        for item in soup.select("ul.board-list li, div.board-list-item"):
            a = item.select_one("a[href]")
            if not a:
                continue
            title_el = item.select_one(".title, .subject, strong")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            date_el = item.select_one(".date, .regdate, span.date")
            raw_d = date_el.get_text(strip=True) if date_el else ""
            if not title or not _is_today(raw_d):
                continue
            href = a["href"]
            url = href if href.startswith("http") else f"{base}{href}"
            results.append(_item("두산", ctype, title, raw_d, url))
    return results


# ──────────────────────────────
# SSG랜더스 (Playwright)
# ──────────────────────────────
async def _crawl_ssg(browser: Browser) -> list[dict]:
    import re as _re
    results = []
    base = "https://www.ssglanders.com"
    soup = await _get_playwright(f"{base}/media/news", browser)
    if not soup:
        return results
    for card in soup.find_all("div", onclick=_re.compile(r"/media/news/detail")):
        onclick = card.get("onclick", "")
        href_match = _re.search(r"location\.href='([^']+)'", onclick)
        if not href_match:
            continue
        href = href_match.group(1)
        url = f"{base}{href}"
        parent = card.parent
        title_el = parent.select_one("h4.text-dotdotdot")
        title = title_el.get_text(strip=True) if title_el else ""
        date_el = parent.find("div", style=_re.compile(r"font-size:\s*12px"))
        raw_d = date_el.get_text(strip=True) if date_el else ""
        if not title or not _is_today(raw_d):
            continue
        results.append(_item("SSG", "notice", title, raw_d, url))
    return results


# ──────────────────────────────
# 한화이글스 (Playwright)
# ──────────────────────────────
async def _crawl_hanwha(browser: Browser) -> list[dict]:
    results = []
    base = "https://www.hanhwaeagles.com"
    for ctype, path in [("notice", "/post/notice"), ("event", "/post/event")]:
        soup = await _get_playwright(f"{base}{path}", browser)
        if not soup:
            continue
        for item in soup.select("ul.post-list li, div.post-item"):
            a = item.select_one("a[href]")
            if not a:
                continue
            title_el = item.select_one(".title, .subject, strong")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            date_el = item.select_one(".date, .regdate")
            raw_d = date_el.get_text(strip=True) if date_el else ""
            if not title or not _is_today(raw_d):
                continue
            href = a["href"]
            url = href if href.startswith("http") else f"{base}{href}"
            results.append(_item("한화", ctype, title, raw_d, url))
    return results


# ──────────────────────────────
# NC다이노스 (Playwright)
# ──────────────────────────────
async def _crawl_nc(browser: Browser) -> list[dict]:
    results = []
    base = "https://www.ncdinos.com"
    for ctype, path in [("notice", "/community/notice"), ("event", "/community/event")]:
        soup = await _get_playwright(f"{base}{path}", browser)
        if not soup:
            continue
        for item in soup.select("ul.board-list li, div.board-item"):
            a = item.select_one("a[href]")
            if not a:
                continue
            title_el = item.select_one(".title, .subject, strong")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            date_el = item.select_one(".date, span.date")
            raw_d = date_el.get_text(strip=True) if date_el else ""
            if not title or not _is_today(raw_d):
                continue
            href = a["href"]
            url = href if href.startswith("http") else f"{base}{href}"
            results.append(_item("NC", ctype, title, raw_d, url))
    return results


# ──────────────────────────────
# KT위즈 (Playwright)
# ──────────────────────────────
async def _crawl_kt(browser: Browser) -> list[dict]:
    results = []
    base = "https://www.ktwiz.co.kr"
    for ctype, path in [("notice", "/media/notice"), ("event", "/media/event")]:
        soup = await _get_playwright(f"{base}{path}", browser)
        if not soup:
            continue
        for item in soup.select("ul.board-list li, div.notice-item"):
            a = item.select_one("a[href]")
            if not a:
                continue
            title_el = item.select_one(".title, .subject, strong")
            title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
            date_el = item.select_one(".date, span.date")
            raw_d = date_el.get_text(strip=True) if date_el else ""
            if not title or not _is_today(raw_d):
                continue
            href = a["href"]
            url = href if href.startswith("http") else f"{base}{href}"
            results.append(_item("KT", ctype, title, raw_d, url))
    return results


# ──────────────────────────────
# KIA타이거즈 (Playwright)
# ──────────────────────────────
async def _crawl_kia(browser: Browser) -> list[dict]:
    results = []
    base = "https://tigers.co.kr"
    soup = await _get_playwright(f"{base}/contents/news", browser, wait_ms=4000)
    if not soup:
        return results
    for item in soup.select("li"):
        a = item.select_one('a[href*="/contents/news/"]')
        if not a or a["href"] == "/contents/news/":
            continue
        title_el = item.select_one("dl dt")
        date_el = item.select_one("dd.icoWrap span.date")
        title = title_el.get_text(strip=True) if title_el else ""
        raw_d = date_el.get_text(strip=True) if date_el else ""
        if not title or not _is_recent(raw_d, days=3):
            continue
        href = a["href"]
        url = href if href.startswith("http") else f"{base}{href}"
        results.append(_item("KIA", "notice", title, raw_d, url))
    return results


async def _get_playwright(url: str, browser: Browser, wait_ms: int = 3000) -> BeautifulSoup | None:
    page = await browser.new_page(extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"})
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(wait_ms)
        html = await page.content()
        return BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.error("Playwright fetch failed [%s]: %s", url, e)
        return None
    finally:
        await page.close()


# ──────────────────────────────
# Playwright 스레드 실행 헬퍼 (Windows uvicorn 이벤트루프 충돌 방지)
# ──────────────────────────────
async def _run_playwright_crawlers() -> list[dict]:
    pw_crawlers = [
        ("LG", _crawl_lg),
        ("두산", _crawl_doosan),
        ("SSG", _crawl_ssg),
        ("한화", _crawl_hanwha),
        ("NC", _crawl_nc),
        ("KT", _crawl_kt),
        ("KIA", _crawl_kia),
    ]
    items: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for team_name, crawler_fn in pw_crawlers:
                try:
                    result = await crawler_fn(browser)
                    items.extend(result)
                    logger.info("%s: %d items", team_name, len(result))
                except Exception as e:
                    logger.error("%s crawler error: %s", team_name, e)
        finally:
            await browser.close()
    return items


def _run_playwright_sync() -> list[dict]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run_playwright_crawlers())
    except Exception as e:
        logger.error("Playwright thread error: %s", e)
        return []
    finally:
        loop.close()


# ──────────────────────────────
# 통합 수집
# ──────────────────────────────
async def crawl_all_team_news() -> list[dict]:
    """모든 구단 당일 공지/이벤트 수집."""
    all_items: list[dict] = []

    # BS4 크롤러 (httpx)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        bs4_results = await asyncio.gather(
            _crawl_kiwoom(client),
            _crawl_lotte(client),
            return_exceptions=True,
        )
    for r in bs4_results:
        if isinstance(r, Exception):
            logger.error("BS4 crawler error: %s", r)
        else:
            all_items.extend(r)

    # Playwright 크롤러 — Windows uvicorn ProactorEventLoop 충돌 방지를 위해 별도 스레드에서 실행
    try:
        loop = asyncio.get_event_loop()
        pw_items = await loop.run_in_executor(None, _run_playwright_sync)
        all_items.extend(pw_items)
    except Exception as e:
        logger.error("Playwright session failed: %s", e)

    logger.info("Team news crawled: %d items (date: %s)", len(all_items), _today())
    return all_items
