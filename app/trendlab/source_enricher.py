"""Fetch article bodies before writing a blog post; snippets remain a fallback."""
import asyncio
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.trendlab.relevance import RELEVANT_THRESHOLD, score_source
from utils.cache import TTLCache

logger = logging.getLogger(__name__)
_cache = TTLCache(default_ttl=1800)
_slots = asyncio.Semaphore(3)
_MAX_BYTES = 2_000_000
_MAX_CHARS = 6000
_SELECTORS = (
    '#dic_area', '#newsct_article', '#article-view-content-div', '#articleBody',
    '#article_body', '#newsEndContents', '#news_body_area', '.article-body',
    '.article_body', '.article_view', '.view_con', '.news_txt',
    '[itemprop="articleBody"]', 'article',
)


def extract_article_text(html: str | bytes) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    candidates = []

    def collect(node):
        if isinstance(node, dict):
            body = node.get('articleBody')
            if isinstance(body, str):
                candidates.append(BeautifulSoup(body, 'html.parser').get_text(' ', strip=True))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            collect(json.loads(script.get_text()))
        except (ValueError, TypeError):
            pass
    for node in soup.select('script, style, nav, footer, aside, form, iframe, .ad, .advertisement, .related, .copyright'):
        node.decompose()
    # Prefer a specific body container over the entire page (menus/related stories).
    for selector in _SELECTORS:
        nodes = soup.select(selector)
        texts = [node.get_text(' ', strip=True) for node in nodes]
        texts = [text for text in texts if len(text) >= 300]
        if texts:
            candidates.append(max(texts, key=len))
            break
    text = max(candidates, key=len, default='')
    return re.sub(r'\s+', ' ', text).strip()[:_MAX_CHARS]


async def _public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 80, 443):
            return False
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM), timeout=3)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (ValueError, OSError, TimeoutError):
        return False


async def _fetch_body(client: httpx.AsyncClient, url: str) -> str:
    cached = _cache.get(url)
    if cached is not None:
        return cached
    body = ''
    try:
        async with _slots, asyncio.timeout(15):
            current = url
            for _ in range(4):
                if not await _public_url(current):
                    break
                async with client.stream('GET', current) as response:
                    if response.is_redirect:
                        current = urljoin(current, response.headers.get('location', ''))
                        continue
                    response.raise_for_status()
                    if 'html' not in response.headers.get('content-type', ''):
                        break
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_BYTES:
                            raise ValueError('article response too large')
                    body = extract_article_text(bytes(chunks))
                    break
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        logger.info('TrendLab article fetch unavailable: %s (%s)', url, type(exc).__name__)
    _cache.set(url, body, ttl=1800 if body else 120)
    return body


async def prepare_sources(topic: str, source_items: list[dict]) -> list[dict]:
    """At most two bounded batches; one shared connection pool for all articles."""
    items = [dict(item) for item in source_items]
    attempted = set()

    async def enrich(client, candidates):
        # General articles before photo captions/breaking headlines; diversify publishers.
        ranked = sorted(candidates, key=lambda s: (
            bool(re.search(r'\[(?:사진|포토|속보|1보)\]|\[포토\]', s.get('title', ''))),
            -len(s.get('description') or ''),
        ))
        selected, hosts = [], set()
        for item in ranked:
            url = item.get('url') or ''
            host = urlsplit(url).hostname
            if item.get('source') != 'naver' or not host or url in attempted or host in hosts:
                continue
            if score_source(topic, item)['relevance_score'] < RELEVANT_THRESHOLD:
                continue
            hosts.add(host)
            attempted.add(url)
            selected.append(item)
            if len(selected) == 5:
                break
        bodies = await asyncio.gather(*(_fetch_body(client, item['url']) for item in selected))
        for item, body in zip(selected, bodies):
            if len(body) >= max(400, len(item.get('description') or '') + 100):
                item['snippet'] = item.get('description', '')
                item['article_text'] = body
                # Every existing grounding/novelty check must see the same evidence as the writer.
                item['description'] = body

    async with httpx.AsyncClient(timeout=8, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'ko-KR,ko;q=0.9'}) as client:
        await enrich(client, items)
        if sum(len(i.get('article_text', '')) for i in items) < 3500:
            from services.naver_api import search_news
            from app.trendlab.freshness import parse_to_utc, is_collectible
            try:
                news = await search_news(topic, display=15, sort='sim')
                urls = {i.get('url') for i in items}
                for article in news:
                    url = article.get('originallink') or article.get('link', '')
                    if not url or url in urls:
                        continue
                    published = parse_to_utc(article.get('pub_date', ''))
                    if not is_collectible(published):
                        continue
                    item = {
                        'source': 'naver', 'title': article.get('title', ''),
                        'description': article.get('description', ''), 'url': url,
                        'publisher': urlsplit(url).hostname or '',
                        'published_at': published.isoformat() if published else None,
                    }
                    if score_source(topic, item)['relevance_score'] >= RELEVANT_THRESHOLD:
                        items.append(item)
                        urls.add(url)
                await enrich(client, items)
            except Exception as exc:
                logger.warning('TrendLab body supplement failed: %s', type(exc).__name__)
    # Keep fetched bodies inside the fact extractor's 15-source budget.
    items.sort(key=lambda i: (bool(i.get('article_text')), i.get('published_at') or ''), reverse=True)
    items = items[:15]
    logger.info('TrendLab full articles %r: fetched=%d chars=%d', topic,
                sum(bool(i.get('article_text')) for i in items), sum(len(i.get('article_text', '')) for i in items))
    return items
