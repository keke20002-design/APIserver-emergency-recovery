"""TrendLab 본문 출처 표기 — "KBS에 따르면"이 문장마다 반복되는 문제 회귀 테스트.

출처 추적(source_id/source_url, 하단 출처 박스)은 유지하고, 본문의 기계적인 출처명 반복만 줄인다.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.trendlab.issue_generator as ig
from app.trendlab.relevance import RelevanceGateResult

SOURCES = [
    {"source": "naver", "title": "아이유 신곡 발표", "description": "", "published_at": "2026-09-10T00:00:00+00:00",
     "publisher": "news.kbs.co.kr", "url": "https://news.kbs.co.kr/1"},
    {"source": "naver", "title": "아이유 뮤직비디오 공개", "description": "", "published_at": "2026-09-10T01:00:00+00:00",
     "publisher": "sbs.co.kr", "url": "https://sbs.co.kr/2"},
]
ANALYSIS = {"unconfirmed": [], "statements": []}


def _pp(content: str, category_is_political: bool = False, sources=SOURCES) -> str:
    return ig._postprocess_content(content, ANALYSIS, category_is_political, sources)


# ── 프롬프트 ────────────────────────────────────────────────────────────────

def test_prompt_no_longer_recommends_inline_source_naming():
    # 예전 규칙 9는 "KBS는 15일 보도에서 ~라고 전했다"처럼 쓰라고 권장했다 — 반복의 직접 원인.
    assert "KBS는 15일 보도에서" not in ig._ISSUE_PROMPT.split("나쁜 예")[0]
    assert "출처 반복 표기 금지" in ig._ISSUE_PROMPT
    for phrase in ("KBS에 따르면", "모닝와이드에 따르면", "보도에 따르면"):
        assert phrase in ig._ISSUE_PROMPT  # 금지 표현으로 명시돼 있어야 한다
    assert "source_id" in ig._ISSUE_PROMPT  # 내부 추적은 유지한다고 명시
    ig._ISSUE_PROMPT.format(  # 중괄호 이스케이프가 깨지지 않았는지
        topic="t", category="c", today="d", source_block="", facts_block="", statements_block="",
        unconfirmed_block="", sides_block="", controversy_core="", why_trending="", current_reaction="",
        qa_block="", entities_block="", background_block="", timeline_block="", length_target_block="",
        rule_1="", flow_guide="", components_guide="",
    )


# ── 후처리: 단순 연예 뉴스 ───────────────────────────────────────────────────

def test_entertainment_repeated_lead_ins_are_reduced():
    content = (
        "<p>KBS에 따르면 아이유가 1년 만에 신곡을 발표했다.</p>"
        "<p>모닝와이드에 따르면 뮤직비디오에는 김영옥이 출연한다.</p>"
        "<p>KBS에 따르면 유인나와의 인연도 알려졌다.</p>"
        "<p>KBS에 따르면 곡에 담긴 배경은 두 사람의 오랜 우정이다.</p>"
        "<p>KBS 보도에 따르면 뮤직비디오는 경찰 컨셉이다.</p>"
    )
    out = _pp(content)
    assert out.count("KBS") == 2  # 글 전체 최대 2회
    assert "아이유가 1년 만에 신곡을 발표했다." in out  # 첫 lead-in은 유지, 문장 본체는 그대로
    # 사실 문장은 하나도 사라지지 않는다
    for fact in ("김영옥이 출연한다", "유인나와의 인연도 알려졌다", "오랜 우정이다", "경찰 컨셉이다"):
        assert fact in out
    assert not ig._over_repeated_sources(out, SOURCES)


def test_same_source_at_most_once_per_paragraph():
    out = _pp("<p>KBS에 따르면 아이유가 신곡을 냈다. KBS에 따르면 김영옥도 출연했다.</p>")
    assert out.count("KBS에 따르면") == 1
    assert "김영옥도 출연했다" in out


def test_generic_lead_ins_are_always_removed():
    out = _pp("<p>보도에 따르면 아이유가 신곡을 냈다. 해당 매체에 따르면 김영옥도 출연했다.</p>")
    assert "따르면" not in out
    assert "아이유가 신곡을 냈다" in out and "김영옥도 출연했다" in out


def test_non_media_evidence_attributions_are_untouched():
    text = "<p>조사에 따르면 60%가 찬성했다.</p><p>조사에 따르면 반대는 20%였다.</p><p>조사에 따르면 나머지는 무응답이다.</p>"
    assert _pp(text) == text
    police = "".join(f"<p>경찰에 따르면 사실{i}이다.</p>" for i in range(4))
    assert _pp(police) == police


# ── 후처리: 직접 인용 / 매체 간 충돌 / 단독 보도는 유지 ────────────────────────

def test_direct_quote_attribution_is_kept():
    quotes = (
        '<p>KBS에 따르면 아이유는 "이번 곡은 오랜 친구를 생각하며 썼다"고 말했다.</p>'
        '<p>KBS에 따르면 김영옥은 "촬영 현장이 유난히 즐거웠다"고 밝혔다.</p>'
    )
    out = _pp("<p>KBS에 따르면 A다.</p><p>KBS에 따르면 B다.</p>" + quotes)
    assert 'KBS에 따르면 아이유는 "이번 곡' in out
    assert 'KBS에 따르면 김영옥은 "촬영 현장' in out


def test_conflicting_reports_keep_attribution():
    conflict = "<p>SBS에 따르면 발매일은 10일이지만, 반면 KBS에 따르면 11일이다.</p>"
    out = _pp("<p>SBS에 따르면 X다.</p><p>SBS에 따르면 Y다.</p>" + conflict)
    assert "SBS에 따르면 발매일은 10일" in out and "KBS에 따르면 11일" in out


def test_exclusive_report_keeps_attribution():
    filler = "<p>KBS에 따르면 A다.</p><p>KBS에 따르면 B다.</p>"
    out = _pp(filler + "<p>KBS에 따르면 이 소식은 KBS가 단독 보도했다.</p>")
    assert "KBS에 따르면 이 소식은 KBS가 단독 보도했다" in out


# ── 정치 공방: 발언 주체는 그대로 ────────────────────────────────────────────

def test_political_speaker_attribution_is_preserved():
    content = (
        "<p>한동훈 의원은 7일 국회에서 의혹을 제기했다고 주장했다.</p>"
        "<p>김승원 후보자 측은 이를 부인하며 사실무근이라고 반박했다.</p>"
        "<p>KBS에 따르면 한동훈 의원은 청문회에서 자료를 공개했다.</p>"
        "<p>KBS에 따르면 김승원 후보자는 자료가 왜곡됐다고 해명했다.</p>"
        "<p>KBS에 따르면 여야는 공방을 이어갔다.</p>"
    )
    out = _pp(content, category_is_political=True)
    assert "한동훈 의원은 7일 국회에서 의혹을 제기했다고 주장했다" in out
    assert "김승원 후보자 측은 이를 부인하며 사실무근이라고 반박했다" in out
    assert "한동훈 의원은 청문회에서 자료를 공개했다" in out  # 매체 lead-in만 빠지고 발언 주체는 남는다
    assert "김승원 후보자는 자료가 왜곡됐다고 해명했다" in out
    assert out.count("KBS에 따르면") == 2
    assert "이 글에서 확인한 주요 출처" in out  # 출처 박스(하단)는 그대로 붙는다


# ── 스포츠 / 행사 ─────────────────────────────────────────────────────────────

def test_sports_and_event_news_lose_repeated_attribution():
    sports = (
        "<p>SBS에 따르면 손흥민이 후반 12분 결승골을 넣었다.</p>"
        "<p>SBS에 따르면 토트넘은 2대1로 승리했다.</p>"
        "<p>SBS에 따르면 다음 경기는 주말이다.</p>"
        "<p>SBS 보도에 따르면 부상자는 없다.</p>"
    )
    out = _pp(sports)
    assert out.count("SBS") <= 2
    assert "결승골을 넣었다" in out and "2대1로 승리했다" in out and "부상자는 없다" in out

    event = "".join(f"<p>연합뉴스에 따르면 행사{i}가 열린다.</p>" for i in range(5))
    out = _pp(event)
    assert out.count("연합뉴스") <= 2
    for i in range(5):
        assert f"행사{i}가 열린다" in out


# ── 주어형 반복은 재작성 대상으로 잡힌다 ──────────────────────────────────────

def test_subject_form_repetition_is_detected():
    content = (
        "<p>KBS는 뮤직비디오 내용도 전했다.</p><p>KBS는 출연진도 밝혔다.</p>"
        "<p>모닝와이드에서는 촬영 뒷이야기를 소개했다.</p><p>KBS는 곡의 배경도 보도했다.</p>"
    )
    out = _pp(content)  # 주어형은 문장이 깨지므로 자동 제거하지 않는다
    assert "KBS는 뮤직비디오 내용도 전했다" in out
    assert ig._over_repeated_sources(out, SOURCES) == {"kbs": 3}


def test_source_grounding_data_is_untouched_by_postprocess():
    items = [dict(i) for i in SOURCES]
    _pp("<p>KBS에 따르면 A다.</p>" * 4, sources=items)
    assert items == SOURCES  # source_url / publisher 메타데이터는 그대로


# ── 재작성 재시도 (generate_issue_post) ────────────────────────────────────────

def _gemini_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps({
        "title": "아이유 신곡 발표", "meta_description": "m", "tags": ["아이유"],
        "summary": ["a", "b", "c"], "content": content,
    })
    return resp


async def _run_generate(first: str, second: str | None) -> tuple[dict, int]:
    async def fake_analyze_event(topic, category, source_items):
        return {
            "facts": [{"fact": "아이유가 신곡을 발표했다", "importance": 95, "source_id": "1"}],
            "statements": [], "unconfirmed": [], "sides": [], "controversy_core": "", "why_trending": "",
            "current_reaction": "", "qa": [], "entities": [], "background": [], "timeline": [],
        }

    responses = [_gemini_response(first)] + ([_gemini_response(second)] if second else [])
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=responses)
    gate = RelevanceGateResult(
        status="ok", relevant_sources=list(SOURCES), source_count=2, relevant_source_count=2,
        strong_relevant_source_count=2, average_relevance=90.0,
    )
    with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
         patch("app.trendlab.fact_extractor.analyze_event", side_effect=fake_analyze_event), \
         patch.object(ig, "gate_sources", return_value=gate), \
         patch("google.genai.Client", return_value=fake_client):
        result = await ig.generate_issue_post("아이유 신곡 발표", "연예", list(SOURCES))
    return result, fake_client.aio.models.generate_content.await_count


REPEATED = "".join(f"<p>KBS는 사실{i}을 전했다.</p>" for i in range(4))
NATURAL = (
    "<p>아이유가 신곡을 발표했다. 사실0과 사실1이 함께 알려졌고, 관련 내용이 하나씩 공개됐다.</p>"
    "<p>이어 사실2와 사실3도 확인되면서 이번 컴백에 대한 구체적인 그림이 한층 또렷해졌다.</p>"
)


@pytest.mark.asyncio
async def test_subject_form_repetition_triggers_one_rewrite_and_uses_better_draft():
    result, calls = await _run_generate(REPEATED, NATURAL)
    assert calls == 2
    assert "KBS는" not in result["content"]


@pytest.mark.asyncio
async def test_rewrite_that_is_not_better_is_discarded():
    result, calls = await _run_generate(REPEATED, REPEATED + "<p>KBS는 사실4를 전했다.</p>")
    assert calls == 2
    assert result["content"].count("KBS는") == 4  # 첫 초안 유지


@pytest.mark.asyncio
async def test_clean_article_needs_no_rewrite():
    result, calls = await _run_generate(NATURAL, None)
    assert calls == 1
    assert result["content"]
