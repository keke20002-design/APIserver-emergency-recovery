"""TrendLab 기사 구성 — "사실을 하나씩 요약하고 끝나는 뉴스 요약문"이 아니라 정보가 이어지는 글이 되는지.

fixture(tests/fixtures/trendlab_trump_iran.json)는 실제 생성 사례다: 같은 입력(source_items/analysis)으로
수정 전 generator가 만든 글(before)과 수정 후 generator가 만든 글(after)을 함께 담았다.
새 fact schema/scoring 없이, 이미 추출된 데이터를 글로 구성하는 방식(프롬프트 + 후처리)만 검증한다.
"""
import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.trendlab.issue_generator as ig
from app.trendlab.relevance import RelevanceGateResult

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "trendlab_trump_iran.json").read_text(encoding="utf-8"))
ANALYSIS = FIXTURE["analysis"]
SOURCES = FIXTURE["source_items"]
GROUNDED = ig._grounded_stems(SOURCES, ANALYSIS)
BEFORE = FIXTURE["before"]["content"]
AFTER = FIXTURE["after"]["content"]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ── fixture: 수정 전 글의 문제가 검출/제거되는가 ───────────────────────────────

def test_before_article_restating_faq_wrap_and_why_paragraph_are_removed():
    body, repeated = ig._prune_redundant_body(BEFORE, GROUNDED)
    boxes, faq_dropped, _ = ig._prune_restating_boxes(body, GROUNDED)
    final = ig._drop_orphan_headings(boxes)
    assert repeated >= 1                                  # "왜 지금 다시 화제가 됐나" 재포장 문단
    assert faq_dropped == 5                               # 본문을 질문 형태로 되풀이한 FAQ 5개 전부
    assert "왜 지금 다시 화제가 됐나" not in final       # 내용이 사라진 소제목도 함께 제거
    assert "자주 묻는 질문" not in final
    # 새 정보가 있는 본문은 남는다
    for kept in ("결정 모드", "페제시키안", "아람코", "킹 칼리드", "공습경보"):
        assert kept in final


def test_after_article_has_nothing_left_to_prune():
    body, repeated = ig._prune_redundant_body(AFTER, GROUNDED)
    boxes, faq_dropped, wrap_dropped = ig._prune_restating_boxes(body, GROUNDED)
    assert (repeated, faq_dropped, wrap_dropped) == (0, 0, 0)
    assert _text(ig._drop_orphan_headings(boxes)) == _text(AFTER)   # 삭제된 게 없다


def test_after_article_structure():
    report = ig._article_report(AFTER, SOURCES, ANALYSIS)
    assert report["sections"] >= 3                        # 사실→세부→맥락 섹션 구조
    assert report["faq"] == 0 and not report["why_section"] and not report["wrap_up"]
    assert report["summary_body_novelty"] is None or report["summary_body_novelty"] >= ig._SUMMARY_BODY_MIN_NOVELTY
    assert report["ungrounded_numbers"] == []


def test_before_report_flags_the_old_structure():
    report = ig._article_report(BEFORE, SOURCES, ANALYSIS)
    assert report["faq"] == 5 and report["why_section"] and report["wrap_up"]


# ── 프롬프트에 넘기는 데이터: 사실을 재탕한 FAQ/왜 화제는 넘기지 않는다 ──────────

def test_restating_qa_and_why_trending_are_not_forwarded_to_the_model():
    known = ig._known_stems(ANALYSIS, GROUNDED)
    assert ig._fresh_qa(ANALYSIS["qa"], known, GROUNDED) == []          # fixture의 FAQ 후보 5개는 전부 사실 재탕
    assert ig._fresh_text(ANALYSIS["why_trending"], known, GROUNDED) == ""


def test_qa_with_genuinely_new_information_is_forwarded():
    known = ig._known_stems(ANALYSIS, GROUNDED)
    grounded = GROUNDED | ig._stems("킹 칼리드 공항 재개 일정 오후 세시 항공사 대체편 운항")
    qa = [{"question": "공항 운항은 언제 재개되나?", "answer": "항공사 대체편 운항이 오후 세시 재개 일정이다."}]
    assert ig._fresh_qa(qa, known, grounded) == qa


# ── 후처리: 개별 규칙 ───────────────────────────────────────────────────────

BODY_A = "<h2>사건의 시작</h2><p>트럼프 대통령은 20일 폭스 뉴스 통화에서 이란 대응을 결정 모드라고 밝혔다. 선택지는 대규모 공격과 경제 압박, 합의다.</p>"


def test_why_trending_section_that_restates_is_dropped_but_new_context_is_kept():
    restating = BODY_A + (
        "<h2>왜 지금 화제가 됐나</h2><p>트럼프 대통령이 이란 대응을 결정 모드라고 밝히고 선택지로 대규모 공격과 "
        "경제 압박, 합의를 제시하면서 관심이 커지고 있습니다.</p>"
    )
    out, n = ig._prune_redundant_body(restating, GROUNDED)
    assert n == 1 and "왜 지금 화제가 됐나" in out          # 문단은 제거 (소제목 정리는 별도 단계)
    assert "왜 지금 화제가 됐나" not in ig._drop_orphan_headings(out)

    new_context = BODY_A + (
        "<h2>이전과 달라진 점</h2><p>후티 반군은 지난 주말 사우디 국영 석유기업 아람코의 연료탱크를 타격했고 "
        "킹 칼리드 공항 항공편이 결항되며 리야드에 처음으로 공습경보가 발령됐다.</p>"
    )
    _, n = ig._prune_redundant_body(new_context, GROUNDED)
    assert n == 0


def test_faq_without_wrapper_box_is_pruned_and_bare_heading_removed():
    pair = ('<div style="margin-bottom:12px;"><p>Q. {q}</p><p>A. {a}</p></div>')
    html = (BODY_A + "🙋 자주 묻는 질문" + pair.format(q="언제 밝혔나?", a="20일 폭스 뉴스 통화에서 밝혔다.")
            + pair.format(q="선택지는?", a="대규모 공격, 경제 압박, 합의다."))
    out, dropped, _ = ig._prune_restating_boxes(html, GROUNDED)
    assert dropped == 2 and "Q." not in out and "자주 묻는 질문" not in out


def test_faq_box_keeps_only_items_with_new_information():
    grounded = GROUNDED | ig._stems("킹 칼리드 공항 재개 일정 오후 세시 항공사 대체편 운항")
    box = ('<div style="background:#f9fafb;"><p>🙋 자주 묻는 질문</p>'
           '<div><p>Q. 언제 밝혔나?</p><p>A. 20일 폭스 뉴스 통화에서 밝혔다.</p></div>'
           '<div><p>Q. 공항은?</p><p>A. 항공사 대체편 운항이 오후 세시 재개 일정이다.</p></div></div>')
    out, dropped, _ = ig._prune_restating_boxes(BODY_A + box, grounded)
    assert dropped == 1 and "공항은?" in out and "언제 밝혔나?" not in out and "자주 묻는 질문" in out


def test_wrap_up_that_only_compresses_the_body_is_dropped():
    wrap = '<div style="background:#1e3a8a;"><p>📝 한 줄 정리</p><p>트럼프 대통령이 이란 대응을 결정 모드라고 밝히며 대규모 공격 경제 압박 합의를 선택지로 제시했다.</p></div>'
    _, _, wrap_dropped = ig._prune_restating_boxes(BODY_A + wrap, GROUNDED)
    assert wrap_dropped == 1


def test_orphan_heading_before_faq_or_end_is_removed_but_heading_with_paragraph_is_kept():
    html = "<h2>내용 있는 제목</h2><p>본문</p><h2>빈 제목</h2>"
    out = ig._drop_orphan_headings(html)
    assert "내용 있는 제목" in out and "빈 제목" not in out


def test_summary_box_is_added_from_summary_field_when_missing_and_not_duplicated():
    body = "<h2>제목</h2><p>본문이다.</p>"
    out = ig._ensure_summary_box(body, ["첫째 줄", "둘째 줄 '인용'", "셋째 줄"])
    assert out.startswith("<div") and "📌 3줄 요약" in out and "'인용'" in out and out.endswith(body)
    assert ig._ensure_summary_box(out, ["다른 줄", "또 다른 줄"]) == out          # 이미 있으면 그대로
    assert ig._ensure_summary_box(body, ["한 줄뿐"]) == body                    # 요약이 부실하면 만들지 않는다


def test_cliche_closers_are_stripped():
    out = ig._strip_cliche_paragraphs("<p>주목받고 있습니다.</p><p>눈길을 끌고 있습니다.</p><p>실제 내용이다.</p>")
    assert out == "<p>실제 내용이다.</p>"


def test_stems_ignore_entities_and_particles():
    assert "39" not in ig._stems("&#39;결정 모드&#39;")
    assert ig._stems("트럼프는 이란을") >= {"트럼프", "이란"}
    assert ig._stems("경제적") == {"경제"}


# ── 프롬프트 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("category,is_political,expected", [
    ("정치", True, "논란/공방"), ("사회", True, "논란/공방"),
    ("연예", False, "새로운 소식"), ("스포츠", False, "경기 결과"), ("IT", False, "단순 뉴스"),
])
def test_flow_guide_matches_article_type_and_prompt_still_formats(category, is_political, expected):
    guide = ig._flow_guide_for(category, is_political)
    assert expected in guide and "fact 1 → 문장" in guide      # 유형별 구조 + 공통 정보 흐름 원칙
    ig._ISSUE_PROMPT.format(
        topic="t", category=category, today="d", source_block="", facts_block="", statements_block="",
        unconfirmed_block="", sides_block="", controversy_core="", why_trending="", current_reaction="",
        qa_block="", entities_block="", background_block="", timeline_block="", length_target_block="",
        rule_1="", flow_guide=guide, components_guide=ig._ISSUE_COMPONENTS_GUIDE,
    )


def test_prompt_makes_recap_sections_optional_and_requires_information_flow():
    p = ig._ISSUE_PROMPT + ig._ISSUE_COMPONENTS_GUIDE
    assert "왜 화제가 됐나 — 기본적으로 만들지 않는 선택 섹션" in p
    assert "FAQ — 기본적으로 선택사항이며 분량 확보용이 아니다" in p
    assert "한줄정리 — 기본적으로 생략한다" in p
    assert "각 문단은 앞 문단에 없던 정보를 최소 하나" in ig._POLITICAL_FLOW
    assert "3줄 요약은 본문을 읽을지 판단하게 하는 preview" in ig._GENERAL_FLOW
    assert "관계" in ig._GENERAL_FLOW


# ── generate_issue_post: TOO_SHORT 재작성은 "안 쓴 정보 + 관계"일 때만 ──────────

def _resp(content: str) -> MagicMock:
    r = MagicMock()
    r.text = json.dumps({"title": "트럼프 대이란 대응 결정 중", "meta_description": "m", "tags": ["트럼프"],
                         "summary": ["a", "b", "c"], "content": content})
    return r


async def _generate(first: str, second: str | None = None) -> tuple[dict, list[str]]:
    async def fake_analyze(topic, category, items):
        return ANALYSIS

    prompts: list[str] = []
    responses = [_resp(first)] + ([_resp(second)] if second else [])

    async def fake_generate(model, contents, config):
        prompts.append(contents)
        return responses.pop(0)

    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=fake_generate)
    gate = RelevanceGateResult(status="ok", relevant_sources=list(SOURCES), source_count=8,
                               relevant_source_count=8, strong_relevant_source_count=4, average_relevance=88.0)

    async def no_supplement(topic, items):
        return items

    with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
         patch("app.trendlab.fact_extractor.analyze_event", side_effect=fake_analyze), \
         patch.object(ig, "gate_sources", return_value=gate), \
         patch.object(ig, "_supplement_if_thin", side_effect=no_supplement), \
         patch("google.genai.Client", return_value=client):
        result = await ig.generate_issue_post(FIXTURE["topic"], FIXTURE["category"], list(SOURCES))
    return result, prompts


@pytest.mark.asyncio
async def test_thin_article_with_unused_source_info_gets_one_relationship_rewrite():
    thin = "<h2>결정 모드</h2><p>트럼프 대통령이 이란에 대한 대응을 결정 중이라고 밝혔다.</p>"
    result, prompts = await _generate(thin, AFTER)
    assert len(prompts) == 2
    retry = prompts[1]
    assert "Do not add unrelated information. Do not repeat existing facts." in retry
    assert "explaining the relationship between those facts" in retry
    assert "페제시키안" in retry.split("재작성 지시")[1]         # 안 쓴 정보를 구체적으로 지목
    assert "페제시키안" in result["content"]                     # 더 나은 두 번째 결과 채택


@pytest.mark.asyncio
async def test_short_article_that_already_uses_the_source_info_is_not_padded_with_a_retry():
    # AFTER는 목표 분량(1700자)보다 짧지만 사전 분석 정보를 이미 다 쓴 글이다 — 다시 시켜봐야 반복만 는다.
    result, prompts = await _generate(AFTER)
    assert len(prompts) == 1
    assert result["content"].startswith("<div")                  # 요약 박스는 유지


@pytest.mark.asyncio
async def test_repetitive_first_draft_is_cleaned_and_faq_never_reaches_the_reader():
    result, prompts = await _generate(BEFORE)
    assert "자주 묻는 질문" not in result["content"]
    assert "왜 지금 다시 화제가 됐나" not in result["content"]
