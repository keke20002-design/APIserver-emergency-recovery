"""TrendLab — 원문에 없는 해설 문장 제거, 안 쓴 정보 회수(TOO_SHORT 재작성), 정보 부족 시 짧게 종료.

목표는 더 긴 기사가 아니다: (1) 입력 데이터에 있는 정보를 쓰지 않고 끝나는 경우와 (2) 원문에 없는 해석·평가
문장이 들어오는 경우만 다룬다. fixture(tests/fixtures/trendlab_trump_iran.json)는 읽기만 한다.
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
AFTER = FIXTURE["after"]["content"]

FACT = "트럼프 대통령은 마수드 페제시키안 이란 대통령과 유엔총회에서 만나는 데 열려있다는 입장을 보였습니다."


def _strip(html: str, sources=SOURCES, analysis=ANALYSIS):
    return ig._strip_ungrounded_commentary(html, sources, analysis, ig._grounded_stems(sources, analysis))


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ── A. 근거 없는 해설 문장 제거 ─────────────────────────────────────────────

def test_a_ungrounded_commentary_sentence_is_removed_and_fact_is_kept():
    out, removed, flagged = _strip(f"<p>{FACT} 이는 복합적인 메시지로 해석됩니다.</p>")
    assert removed == ["이는 복합적인 메시지로 해석됩니다."] and flagged == []
    assert "페제시키안" in out and "해석" not in out


@pytest.mark.parametrize("commentary", [
    "이는 이란에 대한 강경한 대응 의지를 시사합니다.",
    "이번 발언은 미-이란 관계에서 중요한 의미를 갖습니다.",
    "이러한 발언은 국제사회에서 주목받고 있습니다.",
    "양국 관계에 대한 관심이 커지고 있습니다.",
    "이번 회담 여부는 중요한 변수입니다.",
    "현재 이란 정세는 복잡한 양상입니다.",
    "정상회담이 성사될 가능성이 있습니다.",
    "이번 발언은 이란에 강한 메시지를 던졌습니다.",
    "이는 대화의 여지를 남겨둔 것으로 보입니다.",
    "이러한 태도는 대화 의지를 보여줍니다.",
    "이번 사안은 앞으로도 주목할 필요가 있습니다.",
    "이는 미-이란 관계의 불확실성을 더욱 키우고 있습니다.",
])
def test_a_each_commentary_ending_is_detected_and_removed(commentary):
    out, removed, _ = _strip(f"<p>{FACT} {commentary}</p>")
    assert removed == [commentary], commentary
    assert FACT in out


def test_a_paragraph_that_was_only_commentary_disappears_with_its_heading():
    html = f"<h2>사실</h2><p>{FACT}</p><h2>의미</h2><p>이는 복합적인 메시지로 해석됩니다.</p>"
    content, _ = ig._postprocess_content_with_stats(html, ANALYSIS, True, SOURCES)
    assert "복합적인 메시지" not in content and ">의미<" not in content   # 내용 없는 소제목도 함께 정리
    assert "페제시키안" in content


def test_a_plain_facts_and_reported_speech_are_never_touched():
    html = ("<p>트럼프 대통령은 폭스 뉴스와의 통화에서 결정 모드에 있으며 아주 큰 일이 일어날 것이라고 말했습니다. "
            "사우디 수도 리야드에는 처음으로 공습경보가 발령됐습니다. 트럼프 대통령은 이란이 처신을 잘해야 할 것이라고 덧붙였습니다.</p>")
    out, removed, flagged = _strip(html)
    assert (removed, flagged) == ([], []) and out == html


# ── B. 근거 있는 해설은 보존 ────────────────────────────────────────────────

GROUNDED_SOURCES = [{
    "source": "naver", "title": "트럼프 대이란 대응 결정",
    "description": "전문가들은 트럼프 대통령이 군사 공격과 경제 압박, 합의를 동시에 제시한 것은 강경책과 대화를 "
                   "병행하려는 것으로 해석된다고 분석했다.",
    "published_at": "2026-09-20T00:00:00+00:00", "publisher": "", "url": "",
}]
INTERPRETATION = "트럼프 대통령이 군사 공격과 경제 압박, 합의를 동시에 제시한 것은 강경책과 대화를 병행하려는 것으로 해석됩니다."


def test_b_interpretation_present_in_the_source_is_kept():
    analysis = {"unconfirmed": [], "statements": []}
    html = f"<p>트럼프 대통령은 대이란 대응을 결정 중이라고 밝혔습니다. {INTERPRETATION}</p>"
    out, removed, flagged = _strip(html, GROUNDED_SOURCES, analysis)
    assert (removed, flagged) == ([], []) and INTERPRETATION in out


def test_b_same_sentence_without_such_source_is_removed():
    analysis = {"unconfirmed": [], "statements": []}
    plain_sources = [dict(GROUNDED_SOURCES[0], description="트럼프 대통령이 군사 공격과 경제 압박, 합의를 제시했다.")]
    html = f"<p>트럼프 대통령은 대이란 대응을 결정 중이라고 밝혔습니다. {INTERPRETATION}</p>"
    out, removed, _ = _strip(html, plain_sources, analysis)
    assert removed == [INTERPRETATION] and INTERPRETATION not in out


def test_b_interpretation_present_in_the_analysis_data_is_kept():
    analysis = {
        "unconfirmed": [], "statements": [],
        "controversy_core": "트럼프 대통령이 군사 공격과 경제 압박, 합의를 동시에 제시한 것은 강경책과 대화를 병행하려는 것으로 해석된다.",
    }
    html = f"<p>트럼프 대통령은 대이란 대응을 결정 중이라고 밝혔습니다. {INTERPRETATION}</p>"
    out, removed, flagged = _strip(html, [dict(GROUNDED_SOURCES[0], description="")], analysis)
    assert (removed, flagged) == ([], []) and INTERPRETATION in out


def test_b_sentence_mixing_a_fact_with_commentary_keeps_the_fact_and_is_flagged():
    mixed = ("트럼프 대통령은 이란이 처신을 잘해야 할 것이라고 덧붙이며, 향후 이란의 행동에 따라 미국의 대응이 "
             "달라질 수 있음을 강하게 시사했습니다.")
    out, removed, flagged = _strip(f"<p>{FACT} {mixed}</p>")
    assert removed == [] and flagged == [mixed] and mixed in out     # 사실(처신 발언)을 잃지 않는다


# ── C. 안 쓴 정보 회수 (TOO_SHORT 재작성) ────────────────────────────────────

def _resp(content: str, summary=("a", "b", "c")) -> MagicMock:
    r = MagicMock()
    r.text = json.dumps({"title": "트럼프 대이란 대응 결정 중", "meta_description": "m", "tags": ["트럼프"],
                         "summary": list(summary), "content": content})
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


THIN = "<h2>결정 모드</h2><p>트럼프 대통령이 이란에 대한 대응을 결정 중이라고 밝혔습니다.</p>"


@pytest.mark.asyncio
async def test_c_unused_source_info_is_recovered_by_a_targeted_rewrite_not_a_length_request():
    result, prompts = await _generate(THIN, AFTER)
    assert len(prompts) == 2
    note = prompts[1].split("재작성 지시")[1]
    assert "UNUSED INFORMATION:" in note and "페제시키안" in note           # 안 쓴 정보를 항목으로 지목
    assert "Do not repeat existing facts" in note and "explaining the relationship" in note
    assert "전부 넣을 필요는 없다" in note and "글자 수를 채우기 위한 문장을 추가하지 않는다" in note
    assert "원문/분석 데이터에 없는 배경지식, 원인, 전망, 의미, 평가를 새로 만들지 않는다" in note
    assert "이미 설명한 정보는 다시 설명하지 않는다" in note
    for phrase in ig._NON_INFORMATION_PHRASES:                                  # 새 정보가 아닌 표현 목록
        assert phrase in note
    assert not re.search(r"목표 분량|\d+\s*~\s*\d+자|너무 짧", note)               # 분량을 늘리라는 요구 없음
    assert "페제시키안" in result["content"]                                     # 회수된 정보가 반영된 재작성 채택


def test_c_non_information_phrases_are_also_forbidden_in_the_main_prompt():
    for phrase in ("복합적인 메시지로 해석됩니다", "시사합니다", "주목할 필요가 있습니다", "관심이 커지고 있습니다",
                   "중요한 변수입니다", "할 가능성이 있습니다", "여러 가능성을 열어두고 있습니다", "메시지를 던졌습니다"):
        assert phrase in ig._ISSUE_PROMPT, phrase
    assert "길이는 결과이지 목표가 아니다" in ig._ISSUE_PROMPT
    assert "목표 분량까지" not in ig._ISSUE_PROMPT                               # "목표까지 채워라"는 삭제됨


# ── D. 정보가 부족한 기사는 재작성하지 않고 짧게 종료 ──────────────────────────

# 분석 항목을 거의 다 쓴 기사에서 두 항목(처신 발언 / 공습경보)만 뺀 초안 — 안 쓴 정보가 3개 미만이다.
NEARLY_ALL_USED = (
    "<h2>결정 모드</h2><p>도널드 트럼프 미국 대통령이 이란에 대한 대응을 결정 중이라고 밝혔습니다. 폭스 뉴스와의 통화에서 "
    "트럼프 대통령은 자신이 결정 모드에 있으며 아주 큰 일이 머지않은 미래에 일어날 것이라고 말했습니다.</p>"
    "<h2>선택지</h2><p>트럼프 대통령은 대규모 공격, 경제 압박, 합의를 모두 대이란 대응 선택지로 제시했습니다. 트레이 잉스트 "
    "기자는 트럼프 대통령이 이란을 쓸어버리는 것과 경제적으로 고사시키는 것, 그리고 합의에 이르는 것을 가능한 선택지로 "
    "제시했다고 전했습니다.</p>"
    "<p>트럼프 대통령은 마수드 페제시키안 이란 대통령과 유엔총회에서 만나는 데 열려있다는 입장을 보였습니다.</p>"
    "<h2>후티 공격</h2><p>친이란 예멘 후티 반군이 지난 주말 사우디 국영 석유기업 아람코의 연료탱크를 타격했습니다. 후티 반군 "
    "대변인 야히야 사리는 이 공격 사실을 밝혔습니다. 이 공격으로 인근 킹 칼리드 공항 항공편들이 결항되거나 장시간 지연됐습니다. "
    "단순히 미국의 대이란 해상 봉쇄 문제만 있는 것이 아니라 후티가 사우디아라비아를 공격하는 등 중동 지역의 긴장이 고조되고 있습니다.</p>"
)


@pytest.mark.asyncio
async def test_d_article_with_little_unused_information_is_not_rewritten():
    result, prompts = await _generate(NEARLY_ALL_USED)
    assert len(prompts) == 1                                       # 재작성 호출 없이 그대로 종료
    assert ig._visible_text_length(result["content"]) < FIXTURE["richness"]["min_chars"]   # 참고치보다 짧아도 OK
    assert "페제시키안" in result["content"]


@pytest.mark.asyncio
async def test_d_short_article_with_no_unused_information_ends_without_padding():
    result, prompts = await _generate(AFTER)
    assert len(prompts) == 1                                                           # 재작성 호출 없음
    assert len(ig._unused_info(ANALYSIS, result["content"], GROUNDED)) < ig._UNUSED_MIN_ITEMS   # 쓸 정보가 더 없다
    assert ig._visible_text_length(result["content"]) < FIXTURE["richness"]["min_chars"]        # 참고치보다 짧게 끝남


# ── E. 이미 설명한 내용은 "안 쓴 정보"로 다시 넣지 않는다 ────────────────────────

def test_e_information_already_stated_in_the_article_is_not_listed_as_unused():
    article = THIN + f"<p>{FACT} 트럼프 대통령은 폭스 뉴스와의 통화에서 결정 모드에 있으며 아주 큰 일이 일어날 것이라고 말했습니다.</p>"
    unused = ig._unused_info(ANALYSIS, article, GROUNDED)
    joined = " ".join(unused)
    assert "페제시키안" not in joined                        # 이미 쓴 회담 발언
    assert "결정 모드" not in joined                          # 이미 쓴 폭스 뉴스 통화 발언
    assert unused                                              # 아직 안 쓴 정보(후티 공격 등)는 남아 있다
    note = ig._unused_info_rewrite_note(unused)
    assert "페제시키안" not in note and "결정 모드" not in note


def test_e_restated_wording_of_a_used_fact_does_not_count_as_unused():
    # 같은 선택지 사실을 다른 표현으로 이미 쓴 기사 — 표현이 다르다는 이유로 "안 쓴 정보"가 되면 안 된다.
    article = THIN + "<p>트럼프 대통령은 이란을 쓸어버리는 것, 경제적으로 고사시키는 것, 합의에 이르는 것을 선택지로 제시했습니다.</p>"
    joined = " ".join(ig._unused_info(ANALYSIS, article, GROUNDED))
    assert "대규모 공격, 경제 압박, 합의" not in joined and "쓸어버리는 것" not in joined


# ── 해설 혼입 재작성 (사실+해설이 한 문장에 섞여 남은 경우) ──────────────────────

MIXED_DRAFT = (
    "<h2>결정 모드</h2><p>도널드 트럼프 미국 대통령이 이란에 대한 대응을 결정 중이라고 밝혔습니다. 폭스 뉴스와의 통화에서 "
    "트럼프 대통령은 자신이 결정 모드에 있으며 아주 큰 일이 머지않은 미래에 일어날 것이라고 말했습니다.</p>"
    "<h2>선택지</h2><p>트럼프 대통령은 이란을 쓸어버리는 것, 경제적으로 고사시키는 것, 그리고 합의에 이르는 것을 가능한 선택지로 "
    "제시했습니다. 트럼프 대통령은 이란이 처신을 잘해야 할 것이라고 덧붙이며 미국의 대응이 달라질 수 있음을 시사했습니다.</p>"
    "<p>트럼프 대통령은 마수드 페제시키안 이란 대통령과 유엔총회에서 만나는 데 열려있다는 입장을 보였습니다.</p>"
    "<h2>후티 공격</h2><p>친이란 예멘 후티 반군이 지난 주말 사우디 국영 석유기업 아람코의 연료탱크를 타격했습니다. 이 공격으로 "
    "인근 킹 칼리드 공항 항공편들이 결항되거나 장시간 지연됐습니다. 사우디 수도 리야드에도 처음으로 공습경보가 발령되며 "
    "중동 정세의 불안정성을 키우고 있습니다.</p>"
)
CLEAN_REWRITE = (
    MIXED_DRAFT.replace(" 트럼프 대통령은 이란이 처신을 잘해야 할 것이라고 덧붙이며 미국의 대응이 달라질 수 있음을 시사했습니다.",
                        " 트럼프 대통령은 이란이 처신을 잘해야 할 것이라고 덧붙였습니다.")
    .replace("공습경보가 발령되며 중동 정세의 불안정성을 키우고 있습니다.", "공습경보가 발령됐습니다.")
)


@pytest.mark.asyncio
async def test_mixed_fact_and_commentary_sentences_trigger_one_targeted_rewrite():
    result, prompts = await _generate(MIXED_DRAFT, CLEAN_REWRITE)
    assert len(prompts) == 2
    note = prompts[1].split("재작성 지시")[1]
    assert "시사했습니다" in note and "불안정성을 키우고 있습니다" in note      # 문제 문장을 지목
    assert "사실만 남겨라" in note
    assert "시사" not in result["content"] and "불안정성" not in result["content"]
    assert "처신을 잘해야 할 것이라고 덧붙였습니다" in result["content"]        # 사실은 보존


@pytest.mark.asyncio
async def test_rewrite_that_does_not_reduce_commentary_is_discarded():
    result, prompts = await _generate(MIXED_DRAFT, MIXED_DRAFT)
    assert len(prompts) == 2
    assert "시사했습니다" in result["content"]                               # 첫 초안 유지 (사실 손실 없음)


# ── 동의어 재요약 문단 (앞 내용을 다른 표현으로 되풀이) ────────────────────────────

ITEMS = ig._analysis_item_stem_sets(ANALYSIS, GROUNDED)
RECAP = ("<p>도널드 트럼프 미국 대통령은 이란에 대한 대응 방안을 결정하는 단계에 있으며, 군사 공격, 경제 압박, 합의 등 모든 "
         "선택지를 고려하고 있다고 밝혔습니다. 또한 정상회담 가능성도 열어두었습니다.</p>")


def test_synonym_recap_paragraph_is_pruned_but_paragraphs_with_new_facts_are_kept():
    body = (
        "<h2>결정 모드</h2><p>도널드 트럼프 미국 대통령이 이란에 대한 대응을 결정 중이라고 밝혔습니다. 폭스 뉴스와의 통화에서 "
        "결정 모드에 있으며 아주 큰 일이 머지않은 미래에 일어날 것이라고 말했습니다.</p>"
        "<p>트럼프 대통령은 이란을 쓸어버리는 것, 경제적으로 고사시키는 것, 그리고 합의에 이르는 것을 가능한 선택지로 제시했습니다.</p>"
        "<p>트럼프 대통령은 마수드 페제시키안 이란 대통령과 유엔총회에서 만나는 데 열려있다는 입장을 보였습니다.</p>"
        "<h2>후티 공격</h2><p>친이란 예멘 후티 반군이 지난 주말 사우디 국영 석유기업 아람코의 연료탱크를 타격했습니다.</p>"
    )
    without_items, n0 = ig._prune_redundant_body(body + RECAP, GROUNDED)
    with_items, n1 = ig._prune_redundant_body(body + RECAP, GROUNDED, ITEMS)
    assert n0 == 0                                     # 어휘 겹침만으로는 동의어 재요약을 못 잡는다
    assert n1 == 1 and RECAP not in with_items         # 새 분석 항목이 없는 문단이라 제거
    assert _text(with_items) == _text(body)            # 새 사실이 있는 문단은 하나도 안 사라짐


def test_paragraph_carrying_a_still_unused_item_is_never_pruned_as_a_recap():
    body = "<p>도널드 트럼프 미국 대통령이 이란에 대한 대응을 결정 중이라고 밝혔습니다. 폭스 뉴스와의 통화에서 결정 모드에 있으며 아주 큰 일이 일어날 것이라고 말했습니다.</p>"
    fact = "<p>사우디 수도 리야드에는 처음으로 공습경보가 발령됐습니다.</p>"
    out, n = ig._prune_redundant_body(body + fact, GROUNDED, ITEMS)
    assert n == 0 and "공습경보" in out


def test_unconfirmed_remaining_issue_is_kept_as_new_information():
    body = (f"<p>도널드 트럼프 미국 대통령이 이란에 대한 대응을 결정 중이라고 밝혔습니다. 폭스 뉴스와의 통화에서 결정 모드에 "
            f"있으며 아주 큰 일이 일어날 것이라고 말했습니다. {FACT}</p>")
    remaining = "<p>다만 미국과 이란의 정상회담이 실제로 성사될 것인지는 아직 확인되지 않았습니다.</p>"
    out, n = ig._prune_redundant_body(body + remaining, GROUNDED, ITEMS)
    assert n == 0 and "실제로 성사될 것인지" in out


def test_full_postprocess_drops_recap_and_commentary_but_keeps_every_fact_of_the_fixture_article():
    content, stats = ig._postprocess_content_with_stats(AFTER, ANALYSIS, True, SOURCES, None)
    assert stats["commentary_removed"] and stats["repeated_paragraphs"] == 0    # AFTER 안의 해설 문장 3개만 제거
    for fact in ("결정 모드", "페제시키안", "쓸어버리는 것", "처신을 잘해야", "아람코", "킹 칼리드", "공습경보", "야히야 사리"):
        assert fact in content
    assert ig._unused_info(ANALYSIS, content, GROUNDED) == ig._unused_info(ANALYSIS, AFTER, GROUNDED)   # 정보 손실 없음


@pytest.mark.asyncio
async def test_rewrite_that_recovers_info_is_accepted_even_if_one_recap_paragraph_had_to_be_pruned():
    # 실제 Gemini 실행에서 재작성이 안 쓴 정보 8개 중 6개를 회수했는데도, 후처리가 지운 재요약 문단 1개 때문에
    # "흐름 문제가 늘었다"고 판정돼 버려진 사례의 회귀 테스트.
    retry = AFTER + RECAP
    result, prompts = await _generate(THIN, retry)
    assert len(prompts) == 2
    assert "페제시키안" in result["content"] and "아람코" in result["content"]     # 회수된 정보가 반영됨
    assert RECAP not in result["content"]                                          # 재요약 문단은 제거됨


# ── 태그가 깨진 모델 응답 (<p> 누락) ────────────────────────────────────────────

def _without_p_tags(html: str) -> str:
    """실제 Gemini 재시도 응답에서 나온 형태: <h2>는 있는데 문단의 <p> 태그가 없다."""
    return re.sub(r"</?p\b[^>]*>", "", html)


def test_headings_with_bare_text_underneath_are_not_dropped_as_orphans():
    html = "<h2>결정 모드</h2>트럼프 대통령이 대응을 결정 중이라고 밝혔습니다.<h2>후티 공격</h2>아람코 연료탱크가 타격됐습니다."
    assert ig._drop_orphan_headings(html) == html          # 소제목 아래 내용이 있으므로 그대로


def test_well_formed_check_requires_a_paragraph_for_every_heading():
    assert ig._is_well_formed(AFTER)
    assert not ig._is_well_formed(_without_p_tags(AFTER))
    assert not ig._is_well_formed("<h2>제목</h2><h2>제목2</h2>")


def test_bare_text_paragraphs_are_restored_and_well_formed_output_is_left_alone():
    assert ig._wrap_bare_paragraphs(AFTER) == AFTER                       # 정상 출력은 그대로
    repaired = ig._wrap_bare_paragraphs(_without_p_tags(AFTER))
    assert ig._is_well_formed(repaired)
    # 원래 문단 수(8)가 복원된다 — 경계는 "…습니다.이와 함께" 처럼 마침표 뒤에 붙은 글자로 찾는다.
    assert repaired.count("<p>") == sum(1 for k, *_ in ig._scan_units(AFTER) if k == "p")


def test_paragraph_restoration_does_not_split_abbreviations_or_decimals():
    html = "<h2>제목</h2>미국(U.S.) 해군은 www.example.com 에서 3.5% 인상을 밝혔습니다. 이는 단일 문단이다 그대로 유지된다."
    repaired = ig._wrap_bare_paragraphs(html)
    assert repaired.count("<p>") == 1 and "U.S." in repaired and "www.example.com" in repaired and "3.5%" in repaired


def test_commentary_is_still_removed_when_the_model_forgot_the_p_tags():
    # 실제 Gemini 출력에서 <p>가 통째로 빠진 경우 — 복원되지 않으면 해설 검사가 아무것도 못 본다.
    tagless = _without_p_tags(f"<h2>회담</h2><p>{FACT}</p><p>이는 복합적인 메시지로 해석됩니다.</p>")
    content, stats = ig._postprocess_content_with_stats(tagless, ANALYSIS, True, SOURCES)
    assert "복합적인 메시지" not in content and "페제시키안" in content and ">회담<" in content
    assert stats["commentary_removed"] == ["이는 복합적인 메시지로 해석됩니다."]


@pytest.mark.asyncio
async def test_first_draft_without_p_tags_is_repaired_not_left_unchecked():
    result, prompts = await _generate(_without_p_tags(AFTER))
    assert len(prompts) == 1
    assert result["content"].count("<p>") >= 6                             # 문단이 복원됨
    assert "해석" not in result["content"] and "시사" not in result["content"]   # 해설도 검사·제거됨


@pytest.mark.asyncio
async def test_rewrite_without_p_tags_is_repaired_and_then_judged_on_its_merits():
    result, prompts = await _generate(THIN, _without_p_tags(AFTER))
    assert len(prompts) == 2
    assert "페제시키안" in result["content"] and "<p>" in result["content"]   # 회수된 정보가 반영된 재작성 채택


def test_paragraphs_separated_by_br_tags_are_restored():
    # 실제 Gemini 출력 형태: <p> 대신 <br/><br/>로 문단을 나눈다.
    html = ("<h2>결정 모드</h2>" + FACT + "<br/><br/>" + "이는 복합적인 메시지로 해석됩니다." + "<br/><br/>"
            "후티 반군은 지난 주말 아람코 연료탱크를 타격했습니다.")
    repaired = ig._wrap_bare_paragraphs(html)
    assert repaired.count("<p>") == 3 and "<br" not in repaired
    content, stats = ig._postprocess_content_with_stats(html, ANALYSIS, True, SOURCES)
    assert stats["commentary_removed"] == ["이는 복합적인 메시지로 해석됩니다."] and "아람코" in content
