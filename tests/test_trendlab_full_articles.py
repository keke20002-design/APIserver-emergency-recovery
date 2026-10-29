import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.trendlab import source_enricher as se
from app.trendlab import issue_generator as ig
from app.trendlab.fact_extractor import _build_source_block


def test_extracts_body_without_navigation_and_related_news():
    text = '한국 대표팀 선수별 경기 진행과 세부 결과를 설명한다. ' * 25
    html = '<nav>구독 메뉴</nav><div id="article-view-content-div">' + text + '<aside>다른 기사</aside></div>'
    result = se.extract_article_text(html)
    assert '세부 결과' in result
    assert '구독 메뉴' not in result and '다른 기사' not in result
    assert se.extract_article_text('<body>메뉴만 있는 오류 페이지</body>') == ''


def test_extracts_jsonld_article_body():
    html = '<script type="application/ld+json">' + json.dumps({'@graph': [{'articleBody': '경기 본문 ' * 100}]}) + '</script>'
    assert se.extract_article_text(html).startswith('경기 본문')


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_not_fetched():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})
    with patch.object(se, '_public_url', AsyncMock(side_effect=[True, False])):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await se._fetch_body(client, 'https://redirect.test/article') == ''
    assert requests == ['https://redirect.test/article']


@pytest.mark.asyncio
async def test_enrichment_prefers_full_reports_and_shares_client():
    items = [{'source': 'naver', 'title': title, 'url': f'https://news{i}.test/a', 'description': '짧은 요약'}
             for i, title in enumerate(['[사진] 한국 금메달', '한국 금메달 경기 과정'])]
    calls = []
    async def fetch(client, url):
        calls.append((client, url))
        return '확인된 경기 상세 내용 ' * 250
    with patch.object(se, '_fetch_body', side_effect=fetch), patch.object(se, 'score_source', return_value={'relevance_score': 90}):
        result = await se.prepare_sources('한국 금메달', items)
    assert calls[0][1] == 'https://news1.test/a'
    assert calls[0][0] is calls[1][0]
    assert result[0]['snippet'] == '짧은 요약'
    assert len(result[0]['description']) > 2000
    assert items[0]['description'] == '짧은 요약'  # ranking cache remains unchanged
    assert '확인된 경기 상세' in _build_source_block(result)


@pytest.mark.asyncio
async def test_failed_extraction_keeps_snippets_and_supplements_even_with_many_sources():
    items = [{'source': 'naver', 'title': '한국 금메달', 'description': '원래 요약', 'url': f'https://n{i}.test/a'} for i in range(6)]
    with patch.object(se, '_fetch_body', AsyncMock(return_value='')), \
         patch.object(se, 'score_source', return_value={'relevance_score': 90}), \
         patch('services.naver_api.search_news', AsyncMock(return_value=[])) as search:
        result = await se.prepare_sources('한국 금메달', items)
    search.assert_awaited_once()
    assert all(i['description'] == '원래 요약' for i in result)


def test_blog_length_excludes_summary_and_sources():
    html = ig._ensure_summary_box('<h2>제목</h2><p>실제 본문</p>', ['요약 하나', '요약 둘'])
    assert ig._blog_body_length(html) == len('제목실제본문')


def test_new_set_result_survives_repeated_player_and_game_names():
    first = '배재민 선수는 철권 경기에서 일본 선수를 상대로 3세트 승리를 거뒀습니다.'
    second = '배재민 선수는 철권 경기에서 일본 선수를 상대로 7세트 승리를 거뒀습니다.'
    sources = [{'title': '한국 금메달', 'description': first + ' ' + second}]
    grounded = ig._grounded_stems(sources, {})
    result, removed = ig._prune_redundant_body(f'<p>{first}</p><p>{second}</p>', grounded)
    assert '7세트' in result and removed == 0
    assert '7세트' in ig._dedupe_paragraphs(f'<p>{first}</p><p>{second}</p>', grounded)


@pytest.mark.asyncio
async def test_short_full_article_retries_even_without_unused_extraction_items():
    sources = [{'source': 'naver', 'title': '한국 금메달', 'description': '경기 내용 ' * 700,
                'article_text': '경기 내용 ' * 700}]
    analysis = {'facts': [], 'statements': [], 'entities': [], 'background': [], 'timeline': [], 'qa': [],
                'unconfirmed': [], 'sides': [], 'controversy_core': '', 'why_trending': '', 'current_reaction': ''}
    first = '<p>첫 경기 결과</p>'
    second = '<p>' + '선수별 세부 경기 진행과 결과를 설명하는 본문입니다. ' * 30 + '</p>'
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=[
        MagicMock(text=json.dumps({'title': '한국 금메달', 'content': c})) for c in (first, second)])
    gate = MagicMock(status='ok', relevant_sources=sources, relevant_source_count=1, source_count=1, average_relevance=90)
    with patch.dict('os.environ', {'GEMINI_API_KEY': 'test'}), \
         patch.object(ig, '_supplement_if_thin', AsyncMock(return_value=sources)), \
         patch.object(ig, 'gate_sources', return_value=gate), \
         patch('app.trendlab.fact_extractor.analyze_event', AsyncMock(return_value=analysis)), \
         patch('google.genai.Client', return_value=client), \
         patch.object(ig, '_postprocess_content_with_stats', side_effect=lambda c, *a: (c, {'repeated_paragraphs': 0, 'commentary_flagged': []})), \
         patch.object(ig, '_flow_problems', return_value=0), \
         patch.object(ig, '_is_well_formed', return_value=True), \
         patch.object(ig, '_unused_info', return_value=[]):
        result = await ig.generate_issue_post('한국 금메달', '게임', sources)
    assert client.aio.models.generate_content.await_count == 2
    assert '기사 전문을 다시 읽고' in client.aio.models.generate_content.call_args.kwargs['contents']
    assert result['content'] == second
