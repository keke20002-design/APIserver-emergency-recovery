import json
import logging
import os
import random
import re

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"


def _make_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    return genai.Client(api_key=api_key)


def _parse_json_safe(text: str) -> dict:
    """Gemini 응답에서 JSON 안전하게 파싱. 코드블록 제거 후 시도."""
    # 마크다운 코드블록 제거
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # content 필드 안의 개행/따옴표로 인한 파싱 오류 → content만 따로 추출
        title_m = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        meta_m = re.search(r'"meta_description"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        tags_m = re.search(r'"tags"\s*:\s*(\[.*?\])', text, re.DOTALL)
        content_m = re.search(r'"content"\s*:\s*"(.*?)"\s*\}?\s*$', text, re.DOTALL)
        if title_m and content_m:
            content_raw = content_m.group(1).replace('\\"', '"').replace("\\n", "\n")
            tags = json.loads(tags_m.group(1)) if tags_m else []
            return {
                "title": title_m.group(1),
                "meta_description": meta_m.group(1) if meta_m else "",
                "tags": tags,
                "content": content_raw,
            }
        raise

# 소제목 스타일 — 랜덤으로 선택해 프롬프트에 주입
_HEADING_STYLES = [
    "질문형: 각 H2/H3 소제목을 독자가 실제로 궁금해할 질문 형태로 작성. 예) '이번 금리 동결이 내 대출 이자에 미치는 영향은?' / '비트코인이 다시 오르는 진짜 이유는 뭔가?'",
    "핵심요약형: 각 H2/H3 소제목을 해당 섹션의 핵심 내용 + 키워드를 압축한 형태로 작성. 예) '반도체 수출 호조, 코스피 반등의 실질적 근거' / 'GPT-5 출시가 국내 AI 시장 판도를 바꾸는 이유'",
    "단정형: 각 H2/H3 소제목을 강조 포인트를 단언하는 형태로 작성. 예) '지금 체크해야 할 ETF 투자 핵심 3가지' / '이번 정책 변화로 달라지는 부동산 실수요자 전략'",
]

# 후처리: AI 특유의 상투적 소제목 강제 교체
_BAD_HEADER_MAP = {
    "왜 중요한가": "이 이슈가 주목받는 배경",
    "사용자/투자자에게 어떤 영향이 있는가": "실생활·투자에 미치는 파급효과",
    "앞으로 무엇을 봐야 하는가": "앞으로 체크해야 할 핵심 포인트",
    "핵심 요약": "지금 당장 알아야 할 핵심",
    "결론": "정리하며",
    "개요": "배경과 현황",
    "전망": "향후 방향과 시사점",
    "주요 내용": "이번 이슈의 핵심 내용",
    "영향 분석": "실질적 영향 분석",
}


def _fix_ai_headers(content: str) -> str:
    """상투적 AI 소제목 패턴을 후처리로 교체."""
    for bad, good in _BAD_HEADER_MAP.items():
        # h2, h3 태그 안에 정확히 일치하는 경우만 교체
        content = re.sub(
            rf'(<h[23][^>]*>)\s*{re.escape(bad)}\s*(</h[23]>)',
            rf'\g<1>{good}\2',
            content,
            flags=re.IGNORECASE,
        )
    return content


_PROMPT_TEMPLATE = """
당신은 한국 SEO 전문 콘텐츠 에디터다.

키워드: {keyword}
카테고리: {topic}

목표:
검색 사용자의 질문에 가장 도움이 되는 정보성 콘텐츠 작성.

=== SEO 원칙 ===

- 사용자의 검색 의도를 먼저 파악
- 결론과 핵심 정보를 초반에 제공
- 불필요한 서론 제거
- 실제로 도움이 되는 내용 우선
- 키워드 남용 금지
- 자연스러운 한국어 사용

=== 문체 ===

- 친한 전문가가 자기 노트를 독자에게 공유하듯 작성. 기자 말투·강의 말투 절대 금지.
- 절대 강의하지 않는다. 절대 설명하려 들지 않는다. 관찰한 내용을 공유하는 방식으로 쓴다.
- 글은 "전문가가 자기 노트에 기록한 내용을 독자에게 공유하는 느낌"으로 작성.
- 문장 길이를 다양하게 구성
- 동일한 문장 패턴 반복 금지
- 과도한 감탄사·과장 표현 금지
- 첫 문장은 추상적인 사회 이야기 금지. 바로 독자의 관심사로 시작.
  좋은 예: "최근 발표된 정책 중에는 생각보다 혜택이 큰 내용도 있습니다."
  나쁜 예: "우리 사회 곳곳에서 다양한 지원 정책이 이어지고 있습니다."

=== 금지 표현 (절대 사용 불가) ===

- 안녕하세요 / 전문 블로거입니다 / 여러분께
- 살펴보겠습니다 / 알아보겠습니다 / 함께 알아보겠습니다
- 소개해드립니다 / 도움이 되길 바랍니다 / 전달해 드립니다
- 지금부터 / 오늘은 ~ 알아보겠습니다
- 우리 사회 곳곳에서 / 이어지고 있습니다
- 집중하는 한편 / 아끼지 않고 있습니다 / 주목되고 있습니다
- 전달해 드리고자 합니다 / 면밀히 분석 / 한편 / 더욱

=== 권장 표현 ===

보니까 / 실제로 / 의외로 / 생각보다 / 이번에는 / 이 부분은 /
찾아보니 / 개인적으로 / 한 번쯤 / 놓치기 쉬운 / 확인해볼 만한

=== 소제목(H2/H3) 규칙 ===

"왜 중요한가" / "개요" / "결론" / "전망" / "핵심 요약" 같은 추상적 소제목 절대 금지.
반드시 해당 섹션의 핵심 키워드가 포함된 구체적 문장형 소제목으로 동적 생성.

=== 구조 ===

<p>핵심 요약 — 독자가 왜 읽어야 하는지 바로 제시</p>

H2 섹션 3~4개 (위 소제목 규칙 준수)
표(table) 1개 이상 필수, 목록(ul/li) 1개 이상 필수, 비교 정리 권장

=== FAQ (필수 3개 이상) ===

실제 독자가 검색창에 입력할 질문 3개 이상 반드시 작성.

형식:
<div class="faq-item">
<h3 class="faq-q">Q: 질문</h3>
<p class="faq-a">A: 답변</p>
</div>

=== HTML 규칙 ===

본문(content)에 <h1> 금지. <a> 태그 절대 금지 (링크 삽입 금지).
허용: <h2> <h3> <p> <table> <thead> <tbody> <tr> <th> <td> <ul> <li>

=== 길이 ===

1500~3500자 범위

=== 제목 ===

- 25자 이내 (초과 금지)
- 핵심 키워드 포함
- 날짜 포함 금지 / 낚시성 금지
- 아래 고클릭률 패턴 중 하나를 반드시 사용:
  패턴1 "~해보니" 예) Claude Code로 블로그 자동화해보니
  패턴2 "~차이" 예) ChatGPT 유료 무료 차이
  패턴3 "~방법" 예) MCP 서버 구축 방법
  패턴4 "~비용" 예) Gemini API 비용 정리
  패턴5 "~전망" 예) 엔비디아 주가 전망
  패턴6 "~이유" 예) Claude Code가 주목받는 이유

=== 메타 설명 ===

100~160자

=== 태그 ===

3~8개

=== 핵심 요약 (summary) ===

독자가 바로 알아야 할 핵심 포인트 3가지를 15~30자 문장으로 작성.
팩트 기반, 구체적 수치 포함 권장. 투자/행동 유도 문구 금지.

=== 절대 금지 ===

- 안녕하세요
- 오늘은 ~ 알아보겠습니다
- 이 글에서는
- 최고 / 완벽 / 무조건 / 100% 성공
- 허위 경험담
- 존재하지 않는 통계

반드시 JSON만 출력

{{
  "title": "",
  "meta_description": "",
  "tags": [""],
  "summary": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"],
  "content": ""
}}
"""


_ANALYTICAL_PROMPT = """
당신은 해당 분야에 정통한 한국인 전문 블로거다.
독자에게 실질적 도움이 되는 해설형 콘텐츠를 작성한다.

발행 시간대: {slot_label}
오늘 날짜: {today}

=== 수집된 뉴스 기사 ===
{news_text}
=== 뉴스 끝 ===

=== 소제목(H2/H3) 작성 규칙 [최우선] ===

{heading_style}

절대 금지 소제목:
"왜 중요한가" / "사용자/투자자에게 어떤 영향이 있는가" / "앞으로 무엇을 봐야 하는가" /
"개요" / "결론" / "전망" / "핵심 요약" / "주요 내용" / "영향 분석"

→ 이런 추상적·상투적 소제목은 AI가 썼다는 신호다. 반드시 해당 섹션의 핵심 키워드가 포함된
  구체적인 문장형 소제목으로 동적 생성할 것.

=== 뉴스 활용 원칙 [필수] ===

뉴스를 받아도 "기사 요약"을 만들지 않는다. 반드시 아래 4가지를 중심으로 재구성:
1. 왜 중요한가 — 독자에게 직접적으로 연결되는 이유
2. 실제 사용자/투자자에게 어떤 영향이 있는가
3. 활용 방법은 무엇인가 — 구체적 행동 가이드
4. 앞으로 어떻게 될 가능성이 있는가

=== 콘텐츠 흐름 (반드시 이 순서로) ===

1. <p> 첫 문단 — 독자가 왜 읽어야 하는지 바로 (추상적 사회 이야기 금지)
2. H2 섹션 (소제목 규칙 준수)
3. 핵심 수치가 있으면 stat-grid 카드로 표현:
   <div class="stat-grid">
     <div class="stat-item"><h3>수치</h3><p>설명</p></div>
     ...
   </div>
4. 중요 포인트는 point-box로:
   <div class="point-box">💡 핵심 한 문장</div>
5. 작성자 의견은 my-opinion 박스로 (뉴스와 블로그 차이):
   <div class="my-opinion"><h3>✍️ 개인적으로 본 포인트</h3><p>...</p></div>
6. 투자·행동 관련 주제면 checklist 박스 추가:
   <div class="checklist"><h3>✅ 체크포인트</h3><ul><li>...</li></ul></div>
7. 마지막에 반드시 conclusion-box로 마무리:
   <div class="conclusion-box"><h3>한눈에 정리</h3><p>...</p></div>
8. FAQ — 실제 독자가 검색할 질문 3개 이상 (필수)

=== 어조 ===

- 친한 전문가가 자기 노트를 독자에게 공유하듯 작성. 기자 말투·강의 말투 절대 금지.
- 절대 강의하지 않는다. 절대 설명하려 들지 않는다. 관찰한 내용을 공유하는 방식으로 쓴다.
- 글은 "전문가가 자기 노트에 기록한 내용을 독자에게 공유하는 느낌"으로 작성.
- ~입니다 / ~보니까 톤으로 통일
- 뉴스 리포트 말투(~했습니다, ~밝혔습니다 반복) 금지
- 첫 문장은 추상적인 사회 이야기 금지. 바로 독자의 관심사로 시작.
  좋은 예: "6월 들어 신청 가능한 지원금이 꽤 늘었습니다."
  나쁜 예: "우리 사회 곳곳에서 다양한 지원 정책이 이어지고 있습니다."
- 관찰 + 생각 + 경험을 섞어야 블로그 느낌이 남. 예시:
  "요즘 IPO 일정만 봐도 AI 기업 이름이 정말 자주 보입니다.
  불과 2~3년 전만 해도 반도체나 2차전지가 중심이었다면,
  최근에는 AI와 로봇 자동화 기업이 그 자리를 빠르게 차지하고 있는 모습입니다.
  실제로 이번 주 공모 예정 기업들을 살펴보다가 생각보다 AI 관련 기업 비중이 높아서 조금 놀랐습니다."

=== 금지 표현 (절대 사용 불가) ===

- 안녕하세요 / 전문 블로거입니다 / 여러분께
- 살펴보겠습니다 / 알아보겠습니다 / 함께 알아보겠습니다
- 소개해드립니다 / 도움이 되길 바랍니다 / 전달해 드립니다
- 지금부터 / 오늘은 ~ 알아보겠습니다
- 우리 사회 곳곳에서 / 이어지고 있습니다
- 집중하는 한편 / 아끼지 않고 있습니다 / 주목되고 있습니다
- 전달해 드리고자 합니다 / 면밀히 분석 / 한편 / 더욱

=== 권장 표현 ===

보니까 / 실제로 / 의외로 / 생각보다 / 이번에는 / 이 부분은 /
찾아보니 / 개인적으로 / 한 번쯤 / 놓치기 쉬운 / 확인해볼 만한

=== [최우선] 추론·창작 금지 ===

뉴스 기사에 없는 수치·발언·일정·결과 창작 절대 금지.
불확실한 내용 → "~로 알려졌습니다", "~로 전해집니다" 표현 사용.

=== E-E-A-T ===

- 정확성 우선, 사실 기반
- 출처 있는 내용만 단정적 표현
- 최신 정보 반영 (기준: {today})

=== 뉴스 활용 원칙 [최우선] ===

뉴스를 받아도 "기사 요약"을 만들지 않는다. 반드시 아래 4가지를 중심으로 재구성:
1. 왜 중요한가 — 독자에게 직접적으로 연결되는 이유
2. 실제 사용자/투자자에게 어떤 영향이 있는가
3. 활용 방법은 무엇인가 — 구체적 행동 가이드
4. 앞으로 어떻게 될 가능성이 있는가

절대 금지 제목 패턴: "오늘의 ~뉴스", "AI 뉴스 모음", "~뉴스 브리핑", "일일 요약"

=== 제목 패턴 [고클릭률 — 반드시 하나 선택] ===

패턴1 "~해보니" 예) Claude Code로 블로그 자동화해보니
패턴2 "~차이" 예) ChatGPT 유료 무료 차이
패턴3 "~방법" 예) MCP 서버 구축 방법
패턴4 "~비용" 예) Gemini API 비용 정리
패턴5 "~전망" 예) 엔비디아 주가 전망
패턴6 "~이유" 예) Claude Code가 주목받는 이유

=== 키워드 전략 ===

뉴스 제목 그대로 사용 금지. 검색자가 실제로 입력할 검색어 형태로 변환.
예) "엔비디아 실적 발표" → "엔비디아 주가 전망" / "비트코인 급등" → "비트코인 급등 이유"

=== SEO 원칙 ===

- 핵심 정보를 첫 문단에 제공
- 불필요한 서론 제거
- 키워드 남용 금지

=== HTML 규칙 ===

content에 <h1> 금지. <a> 태그 절대 금지 (URL·링크 삽입 금지).
허용 태그: <h2> <h3> <p> <table> <thead> <tbody> <tr> <th> <td> <ul> <li> <mark>
허용 클래스(CSS 미리 정의됨): stat-grid, stat-item, point-box, my-opinion, checklist, conclusion-box, quote-box
- 중요 인용·핵심 한 줄 강조: <div class="quote-box">문장</div>
- 독자가 반드시 기억해야 할 핵심 문구(2~5단어)는 <mark>문구</mark>로 감싸라. 문단당 최대 2개. <strong> 대신 <mark> 사용.
FAQ: <div class="faq-item"><h3 class="faq-q">Q: 질문</h3><p class="faq-a">A: 답변</p></div>

=== 길이 ===

1500~3000자

=== 핵심 요약 (summary) ===

독자가 바로 알아야 할 핵심 포인트 3가지를 15~30자 문장으로 작성.
팩트 기반, 구체적 수치 포함 권장. 투자/행동 유도 문구 금지.

=== 절대 금지 ===

- 안녕하세요 / 이 글에서는 / 오늘은 ~ 알아보겠습니다
- 최고 / 완벽 / 무조건
- 뉴스에 없는 내용 창작
- content 안에 <h1>

반드시 JSON만 출력

{{
  "keyword": "검색어 형태로 변환된 SEO 키워드 (뉴스 제목 아님)",
  "title": "25자 이내, 사람이 실제 검색하는 형태, 핵심 키워드 포함, 낚시성·날짜 금지",
  "meta_description": "100~160자",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "summary": ["핵심 포인트 1 (15~30자)", "핵심 포인트 2", "핵심 포인트 3"],
  "content": "WordPress HTML 본문"
}}
"""

_SLOT_LABELS = {
    "morning": "오전 (AI·기술 분석)",
    "afternoon": "오후 (AI·투자 분석)",
    "evening": "저녁 (AI·시장 분석)",
}


async def generate_analytical_post(news_items: list[dict], slot: str) -> dict:
    """
    뉴스 기사를 해설형 블로그로 생성.
    keyword, title, meta_description, tags, content + keyword 반환.
    """
    from datetime import date
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    if not news_items:
        raise RuntimeError("뉴스 데이터가 없습니다")

    today = date.today().strftime("%Y년 %m월 %d일")
    slot_label = _SLOT_LABELS.get(slot, slot)

    news_text = ""
    for i, item in enumerate(news_items[:10], 1):
        news_text += f"[기사 {i}] {item['title']}\n"
        if item.get("description"):
            news_text += f"내용: {item['description']}\n"
        news_text += "\n"

    client = genai.Client(api_key=api_key)
    heading_style = random.choice(_HEADING_STYLES)
    prompt = _ANALYTICAL_PROMPT.format(
        slot_label=slot_label, today=today, news_text=news_text,
        heading_style=heading_style,
    )
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.75,
            max_output_tokens=8192,
        ),
    )

    try:
        data = _parse_json_safe(response.text)
    except json.JSONDecodeError as e:
        logger.error("Gemini JSON parse failed: %s\nRaw: %s", e, response.text[:500])
        raise RuntimeError(f"Gemini 응답 파싱 실패: {e}")

    required = {"keyword", "title", "meta_description", "tags", "content"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError(f"Gemini 응답에 필드 누락: {missing}")

    data.setdefault("summary", [])

    # 후처리: 상투적 소제목 교체
    data["content"] = _fix_ai_headers(data["content"])

    logger.info(
        "Analytical post generated — slot=%s, style=%s, keyword='%s', title='%s', chars=%d",
        slot, heading_style[:10], data["keyword"], data["title"], len(data["content"]),
    )
    return data


# 이전 버전 호환 유지 (pipeline fallback에서 사용)
async def generate_blog_post_from_news(keyword: str, topic: str, news_items: list[dict]) -> dict:
    return await generate_analytical_post(news_items, slot="evening")


async def generate_blog_post(keyword: str, topic: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    client = genai.Client(api_key=api_key)
    prompt = _PROMPT_TEMPLATE.format(keyword=keyword, topic=topic)
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.85,
            max_output_tokens=8192,
        ),
    )

    try:
        data = _parse_json_safe(response.text)
    except json.JSONDecodeError as e:
        logger.error("Gemini JSON parse failed: %s\nRaw: %s", e, response.text[:500])
        raise RuntimeError(f"Gemini 응답 파싱 실패: {e}")

    required = {"title", "meta_description", "tags", "content"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError(f"Gemini 응답에 필드 누락: {missing}")

    data.setdefault("summary", [])
    data["content"] = _fix_ai_headers(data["content"])
    logger.info(
        "Blog post generated — keyword='%s', title='%s', chars=%d",
        keyword, data["title"], len(data["content"]),
    )
    return data


_CATEGORY_PEXELS_FALLBACKS: dict[str, list[str]] = {
    # WordPress 카테고리 — 로봇/두뇌/사이버펑크 이미지 회피, UI·코드화면·대시보드 지향
    "AI":            ["ai chatbot interface screen", "software dashboard analytics ui", "code editor dark theme", "minimal tech icon flat design", "app interface clean ui"],
    "Automation":    ["workflow diagram nodes screen", "software dashboard automation ui", "code editor terminal screen", "flowchart diagram whiteboard", "app integration icons flat"],
    "Developer":     ["code editor dark theme screen", "terminal command line screen", "software dashboard ui dark", "developer desk dual monitor", "github repository interface"],
    "Business":      ["marketing dashboard analytics screen", "laptop charts graphs desk", "business meeting whiteboard planning", "seo analytics dashboard screen", "team working laptop office"],
    "Investment":    ["stock market chart screen", "financial trading dashboard ui", "investment portfolio graph screen", "stock ticker numbers screen", "finance app mobile screen"],
    "Market":        ["stock market chart screen", "financial trading dashboard ui", "investment portfolio graph screen", "wall street finance building", "stock ticker numbers screen"],
    "Markets":       ["stock market chart screen", "financial trading dashboard ui", "investment portfolio graph screen", "wall street finance building", "stock ticker numbers screen"],
    "Crypto":        ["crypto trading dashboard screen", "blockchain network diagram", "crypto app mobile interface", "digital currency chart screen", "crypto exchange interface screen"],
    "Guides":        ["laptop notebook desk workspace", "learning education desk setup", "productivity workspace clean desk", "open book notebook desk", "step by step checklist screen"],
    "Economy":       ["economy business growth chart", "financial report chart screen", "gdp growth graph screen", "economic analysis dashboard", "business finance chart screen"],
    "Consumer Tech": ["smartphone product display clean", "laptop computer modern desk", "tech gadget product flat lay", "apple device minimal desk", "consumer electronics display"],
    "Trending":      ["breaking news screen laptop", "social media feed interface", "tech news website screen", "digital news feed mobile", "trending headlines screen"],
    "Programming":   ["programming code screen dark", "software developer laptop desk", "code editor terminal window", "coding keyboard dark desk", "developer workspace dual monitor"],
    "Tools":         ["productivity app dashboard screen", "software tools interface laptop", "digital tools workspace desk", "app interface screen clean", "developer tools terminal screen"],
    # Naver 카테고리
    "연예":  ["couple romantic date sunset", "baby first birthday party cake",
              "chef cooking kitchen professional", "concert stage performance lights",
              "detective crime investigation scene", "celebrity award ceremony red carpet",
              "television studio interview microphone", "actor actress emotional scene"],
    "스포츠": ["baseball stadium night game", "soccer player kicking goal", "athlete competition action",
              "sports crowd stadium cheering", "tennis court match play", "basketball game arena"],
    "IT":    ["technology computer screen", "smartphone launch product", "tech startup office team",
              "digital app interface design", "software developer coding screen"],
    "자동차": ["electric vehicle charging station", "modern car highway drive", "car showroom interior",
              "automobile engine close up", "racing car track speed"],
    "주식":  ["stock market chart screen", "financial trading desk monitors", "investment graph rising",
              "wall street finance building", "crypto trading terminal"],
    "이슈":  ["breaking news live broadcast", "social media phone trending", "public protest crowd street",
              "news anchor studio desk", "viral controversy discussion"],
    "생활":  ["morning routine coffee kitchen", "healthy meal food prep", "cozy home interior living",
              "family lifestyle everyday", "urban daily commute city"],
    "쇼핑":  ["online shopping delivery unboxing", "fashion clothes store display", "luxury bag shopping",
              "ecommerce mobile payment", "retail consumer products"],
    "게임":  ["esports tournament arena crowd", "gaming rgb desk setup", "video game controller action",
              "streamer broadcast gaming", "mobile game smartphone play"],
}


_NAVER_CATEGORIES = {"연예", "스포츠", "IT", "자동차", "주식", "이슈", "생활", "쇼핑", "게임"}


_INTENT_SUFFIXES_IMG = [
    "가 이슈인 이유", "이 이슈인 이유", " 이슈인 이유",
    " 반응 총정리", " 상황 정리", " 화제가 된 이유",
    " 앞으로 어떻게 될까", " 핵심 5가지", " 실제 영향은",
    " 논란 총정리", " 솔직한 생각", " 지금 상황",
    "요즘 ",
]

# ── 키워드 직접 매핑 (Gemini 없이 Pexels 쿼리 결정) ─────────────────────────
# key: 핵심 주제에 포함되면 매핑 (부분 문자열 매칭, 길이 긴 것 우선)
_KEYWORD_PEXELS_MAP: list[tuple[str, str]] = [
    # ── 스포츠 ──
    ("파크골프",        "park golf outdoor green course"),
    ("골프",           "golf course green fairway"),
    ("축구",           "football soccer match stadium"),
    ("야구",           "baseball game stadium pitcher"),
    ("농구",           "basketball court game dunk"),
    ("배구",           "volleyball match court spike"),
    ("테니스",         "tennis court player racket"),
    ("수영",           "swimming pool athlete race"),
    ("마라톤",         "marathon running race crowd road"),
    ("육상",           "track field athletics sprint"),
    ("격투기",         "martial arts boxing ring fight"),
    ("유도",           "judo wrestling martial arts"),
    ("태권도",         "taekwondo martial arts kick"),
    ("e스포츠",        "esports gaming tournament arena"),
    ("이스포츠",       "esports gaming tournament arena"),
    ("스포츠",         "sports athlete competition stadium"),
    ("올림픽",         "olympic stadium torch ceremony"),
    ("월드컵",         "football world cup stadium fans"),
    ("KBO",           "baseball stadium game crowd"),
    ("K리그",         "football soccer match stadium"),
    ("EPL",           "football soccer match stadium"),
    # ── 연예 — 주요 인물/그룹 ──
    ("아이유",         "singer music stage performance spotlight"),
    ("BTS",           "kpop concert stage crowd lights"),
    ("방탄소년단",     "kpop concert stage crowd lights"),
    ("블랙핑크",       "kpop concert stage crowd lights"),
    ("뉴진스",        "kpop concert stage crowd lights"),
    ("에스파",         "kpop concert stage crowd lights"),
    ("르세라핌",       "kpop concert stage crowd lights"),
    ("세븐틴",        "kpop concert stage crowd lights"),
    ("NCT",           "kpop concert stage crowd lights"),
    ("트와이스",       "kpop concert stage crowd lights"),
    ("태연",          "singer music stage microphone spotlight"),
    ("아이브",        "kpop concert stage crowd lights"),
    ("스트레이키즈",  "kpop concert stage crowd lights"),
    # ── 연예 — 장르 ──
    ("드라마",         "drama film set actress actor"),
    ("영화",           "cinema movie film theater"),
    ("아이돌",         "kpop concert stage performance lights"),
    ("K-pop",         "kpop concert stage performance lights"),
    ("Kpop",          "kpop concert stage performance lights"),
    ("뮤직비디오",     "music video studio filming production"),
    ("콘서트",         "concert stage crowd performance lights"),
    ("레드카펫",       "celebrity award ceremony red carpet"),
    ("시상식",         "award ceremony trophy stage gala"),
    ("방송",           "television studio interview microphone"),
    ("토크쇼",         "television talk show studio host"),
    ("예능",           "television studio entertainment show"),
    ("열애",           "couple romantic date love"),
    ("결혼",           "wedding ceremony couple bride groom"),
    ("이별",           "couple walking apart breakup silhouette"),
    ("임신",           "pregnancy mother baby announcement"),
    ("출산",           "newborn baby mother hospital"),
    ("돌잔치",         "baby first birthday party cake"),
    # ── 음식/생활 ──
    ("파크골프장",     "park golf outdoor green course"),
    ("뷔페",           "buffet restaurant food spread table"),
    ("맛집",           "restaurant food delicious meal plate"),
    ("카페",           "cafe coffee shop interior cozy"),
    ("베이커리",       "bakery bread pastry shop"),
    ("편의점",         "convenience store night shopping"),
    ("다이소",         "retail store shopping discount items"),
    ("쿠팡",           "delivery package online shopping"),
    ("애슐리",         "buffet restaurant salad food spread"),
    # ── EN Hub 전용 (브랜드 색/UI/화면 지향, 로봇·두뇌 이미지 회피) ──
    ("Claude Code",    "code editor terminal dark theme"),
    ("Cursor",         "code editor dark theme screen"),
    ("VSCode",         "code editor blue theme screen"),
    ("GitHub Copilot", "code editor suggestion screen"),
    ("MCP",            "software architecture diagram screen"),
    ("Docker",         "terminal command line screen"),
    ("n8n",            "workflow automation diagram screen"),
    ("Zapier",         "app integration icons flat"),
    ("Make",           "workflow diagram nodes screen"),
    ("OpenRouter",     "api dashboard interface screen"),
    ("Claude",         "chat app interface screen minimal"),
    ("Gemini",         "chat app interface screen minimal"),
    ("Grok",           "chat app interface screen minimal"),
    ("Perplexity",     "search interface screen minimal"),
    ("Nvidia",         "stock chart screen green"),
    ("AMD",            "stock chart screen green"),
    ("AI ETF",         "investment portfolio graph screen"),
    ("OpenAI",         "software dashboard ui screen"),
    ("AI Marketing",   "marketing dashboard analytics screen"),
    ("AI SEO",         "seo analytics dashboard screen"),
    ("AI Ads",         "marketing dashboard analytics screen"),
    ("AI Productivity","productivity app dashboard screen"),
    # ── IT/테크 ──
    ("AI",             "software dashboard ui screen"),
    ("반도체",         "semiconductor chip microprocessor technology"),
    ("스마트폰",       "smartphone mobile phone display hands"),
    ("아이폰",         "iphone smartphone apple display"),
    ("갤럭시",         "samsung galaxy smartphone display"),
    ("노트북",         "laptop computer desk workspace"),
    ("게임",           "gaming computer setup rgb keyboard"),
    ("챗GPT",         "chat app interface screen minimal"),
    ("ChatGPT",       "chat app interface screen minimal"),
    # ── 경제/금융 ──
    ("주식",           "stock market chart investment finance"),
    ("코인",           "cryptocurrency bitcoin digital currency"),
    ("비트코인",       "bitcoin cryptocurrency digital gold"),
    ("ETF",           "stock market investment portfolio chart"),
    ("부동산",         "apartment building real estate city"),
    ("아파트",         "apartment building real estate city"),
    # ── 자동차 ──
    ("전기차",         "electric vehicle car charging modern"),
    ("테슬라",         "electric vehicle car charging modern"),
    ("자동차",         "car automobile road driving"),
    # ── 사회/이슈 ──
    ("화재",           "fire emergency rescue firefighter"),
    ("사고",           "accident emergency police scene"),
    ("시위",           "protest crowd demonstration street"),
    ("선거",           "election voting ballot democracy"),
    ("환경",           "nature environment green sustainability"),
    ("기후",           "climate change environment nature sky"),
    ("범죄",           "police crime investigation detective"),
    ("사건",           "police investigation crime scene"),
    # ── 여행 ──
    ("여행",           "travel destination tourism landscape"),
    ("제주",           "jeju island korea nature beach"),
    ("해외",           "travel abroad airport airplane tourism"),
]


def _direct_pexels_query(core_topic: str) -> str | None:
    """핵심 주제에서 직접 Pexels 쿼리 반환. 매핑 없으면 None."""
    topic_lower = core_topic.lower()
    # 길이 긴 키워드 우선 매칭
    for keyword, query in sorted(_KEYWORD_PEXELS_MAP, key=lambda x: -len(x[0])):
        if keyword.lower() in topic_lower:
            return query
    return None


def _strip_intent_suffix(keyword: str) -> str:
    """이미지 쿼리용: 키워드에서 인텐트 접미사/접두사 제거해 핵심 주제만 추출."""
    kw = keyword.strip()
    if kw.startswith("요즘 "):
        kw = kw[3:]
    for suffix in _INTENT_SUFFIXES_IMG:
        if kw.endswith(suffix):
            return kw[: -len(suffix)].strip()
    return kw


async def _generate_image_search_query(keyword: str, title: str, category: str = "AI", summary: list[str] | None = None, hub: str | None = None) -> str:
    """제목/카테고리 기반 Pexels 영어 쿼리 생성. 직접 매핑 우선, 없으면 Gemini."""
    # 인텐트 접미사 제거
    core_topic = _strip_intent_suffix(keyword)

    # 1순위: 핵심 주제 직접 매핑 (Gemini 불필요) — hub가 있으면 최우선(접두사 유무와 무관하게 정확 매칭)
    direct = (_direct_pexels_query(hub) if hub else None) or _direct_pexels_query(core_topic) or _direct_pexels_query(title)
    if direct:
        logger.info("Pexels direct mapping: '%s' → '%s'", core_topic, direct)
        return direct

    # 1.5순위: 연예 카테고리 + 한글 인물 이름 패턴이면 카테고리 폴백 사용
    # (Gemini가 인물 이름만 보고 열애/커플 이미지로 오판하는 것 방지)
    import re as _re
    if category in ("연예", "스포츠") and _re.match(r"^[가-힣]{2,5}$", core_topic):
        fallback = random.choice(_CATEGORY_PEXELS_FALLBACKS.get(category, _CATEGORY_PEXELS_FALLBACKS["AI"]))
        logger.info("Pexels person-name fallback: '%s' cat=%s → '%s'", core_topic, category, fallback)
        return fallback

    # 2순위: Gemini로 쿼리 생성
    try:
        client = _make_client()
        summary_hint = ""
        if summary:
            summary_hint = f"Post summary: {' / '.join(summary[:2])}\n"
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=(
                f"Generate a 3-5 word English Pexels stock photo search query for this Korean blog post.\n"
                f"Category: {category}\n"
                f"Core topic: {core_topic}\n"
                f"Title: {title}\n"
                f"{summary_hint}\n"
                f"Rules:\n"
                f"- English words only (NO Korean), no explanations, no quotes\n"
                f"- MUST be 3-5 words\n"
                f"- Focus on the CORE TOPIC, ignore intent words like '이슈인 이유', '실제 영향은', '화제가 된 이유'\n"
                f"- Describe the VISUAL SCENE that would appear in a photo matching this post\n"
                f"- Do NOT use person names or brand names — use what they do or what surrounds them\n"
                f"- Do NOT use robot imagery, brain/circuit imagery, or cyberpunk/neon-futuristic scenes\n"
                f"- Prefer: simple UI screenshots, code/terminal screens, dashboards, icons, flat-lay product shots\n\n"
                f"Scene mapping examples (EN tech topic → Pexels query):\n"
                f"  AI chatbot/assistant tool → 'chat app interface screen minimal'\n"
                f"  coding tool/IDE/dev tool → 'code editor dark theme screen'\n"
                f"  automation/workflow tool → 'workflow diagram nodes screen'\n"
                f"  SaaS/dashboard product → 'software dashboard ui screen'\n"
                f"  stock/investment topic → 'stock chart screen green'\n"
                f"  marketing/SEO/ads topic → 'marketing dashboard analytics screen'\n\n"
                f"Scene mapping examples (Korean core topic → Pexels query):\n"
                f"  연인 결별/이별 → 'couple walking apart silhouette'\n"
                f"  연인 열애/커플 → 'happy couple romantic date'\n"
                f"  돌잔치/아기 생일 → 'baby first birthday party cake'\n"
                f"  셰프/요리 방송 → 'chef cooking kitchen professional'\n"
                f"  뷔페/맛집 음식 → 'buffet restaurant food spread'\n"
                f"  범죄/사건/수사 → 'detective crime investigation scene'\n"
                f"  사회 논란/이슈 → 'crowd protest social controversy'\n"
                f"  주식/코인 급락 → 'stock market red chart crash'\n"
                f"  AI/반도체/IT → 'semiconductor chip artificial intelligence'\n"
                f"  K-pop 아이돌 공연 → 'concert stage performance lights'\n"
                f"  배우/드라마 출연 → 'actress drama film set performance'\n"
                f"  K-콘텐츠/한류 영향력 → 'korean entertainment media cultural'\n"
                f"  연예인 수상식/레드카펫 → 'celebrity award ceremony red carpet'\n"
                f"  방송인/MC/토크쇼 → 'television studio interview microphone'\n"
                f"  가수/음악방송 → 'singer music stage microphone spotlight'\n"
                f"  운동선수/스포츠 활약 → 'athlete sports champion trophy'\n"
                f"  스마트폰 신제품 → 'smartphone product launch display'\n"
                f"  자동차/전기차 → 'electric vehicle charging modern car'\n"
                f"  게임/e스포츠/유스 이스포츠 → 'esports youth gaming tournament arena'\n"
                f"  청소년 행사/페스티벌 → 'youth event festival outdoor crowd'\n"
                f"  사회공헌/자선 → 'charity community volunteers helping'\n"
                f"  패션/쇼핑 → 'fashion shopping boutique clothing'\n"
                f"  축구/야구/농구 → 'football baseball basketball stadium'\n"
                f"  음식/맛집/레스토랑 → 'restaurant food delicious meal plate'\n"
                f"  부동산/아파트 → 'apartment building real estate cityscape'\n"
                f"  주가/ETF/투자 → 'stock market investment chart finance'\n\n"
                f"Output: 3-5 English words only, nothing else."
            ),
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=40),
        )
        query = response.text.strip().strip('"').strip("'").split("\n")[0].strip()
        words = query.split()
        if any(ord(c) > 127 for c in query) or len(words) < 3 or len(words) > 7:
            raise ValueError(f"invalid query: '{query}'")
        logger.info("Pexels search query: '%s' (category=%s, keyword=%s)", query, category, keyword[:30])
        return query
    except Exception as e:
        fallbacks = _CATEGORY_PEXELS_FALLBACKS.get(category, _CATEGORY_PEXELS_FALLBACKS["AI"])
        fallback = random.choice(fallbacks)
        logger.debug("Image query fallback (reason=%s, category=%s) → '%s'", e, category, fallback)
        return fallback


_KR_GUIDE_KEYWORDS: list[dict] = [
    # AI 도구 (70%) — 최우선
    {"keyword": "Claude Code 자동화 방법",          "category": "Automation"},
    {"keyword": "ChatGPT 유료 무료 차이",            "category": "AI"},
    {"keyword": "Gemini API 비용 정리",              "category": "AI"},
    {"keyword": "MCP 서버 구축 방법",               "category": "Automation"},
    {"keyword": "Claude Code vs Cursor 차이",        "category": "AI"},
    {"keyword": "AI Agent 만드는 방법",              "category": "Automation"},
    {"keyword": "OpenAI API 사용 방법",              "category": "Guides"},
    {"keyword": "Cursor AI 사용해보니",              "category": "AI"},
    {"keyword": "Gemini 2.5 Flash 써보니",           "category": "AI"},
    {"keyword": "Claude Pro 결제할 가치가 있을까",   "category": "AI"},
    {"keyword": "AI 자동화 사례 정리",               "category": "Automation"},
    {"keyword": "ChatGPT 최신 기능 정리",            "category": "AI"},
    {"keyword": "Claude Code로 블로그 자동화해보니", "category": "Automation"},
    {"keyword": "MCP 활용법 정리",                   "category": "Automation"},
    {"keyword": "Gemini API vs OpenAI API 비교",     "category": "AI"},
    {"keyword": "AI 코딩 어시스턴트 비교",           "category": "Guides"},
    {"keyword": "Claude Code 설치 방법",             "category": "Guides"},
    # 투자 (30%) — 다음 우선순위
    {"keyword": "엔비디아 주가 전망",                "category": "Market"},
    {"keyword": "비트코인 ETF 영향",                 "category": "Crypto"},
    {"keyword": "미국주식 AI 관련주 전망",           "category": "Market"},
    {"keyword": "ETF 투자 방법 정리",                "category": "Market"},
    {"keyword": "AI 수혜주 찾는 방법",               "category": "Market"},
    {"keyword": "반도체 주가 전망",                  "category": "Market"},
]

_KR_GUIDE_PROMPT = """
당신은 "AI + 자동화 + 투자" 전문 블로거다.
검색 유입과 장기 트래픽 확보가 목표다. 뉴스 요약이 아닌, 직접 사용 경험과 분석 중심으로 작성한다.

주제 키워드: {keyword}
카테고리: {category}
오늘 날짜: {today}

=== 제목 패턴 [반드시 하나 선택] ===

패턴1 "~해보니"   예) Claude Code로 블로그 자동화해보니
패턴2 "~차이"     예) ChatGPT 유료 무료 차이
패턴3 "~방법"     예) MCP 서버 구축 방법
패턴4 "~비용"     예) Gemini API 비용 정리
패턴5 "~전망"     예) 엔비디아 주가 전망
패턴6 "~이유"     예) Claude Code가 주목받는 이유

제목 규칙: 25자 이내 / 날짜 금지 / 낚시성 금지 / 핵심 키워드 포함

=== 콘텐츠 구성 원칙 ===

반드시 아래 4가지를 중심으로 작성:
1. 왜 중요한가 — 독자에게 직접 연결되는 이유
2. 실제 사용자/투자자에게 어떤 영향이 있는가
3. 활용 방법은 무엇인가 — 구체적 행동 가이드
4. 앞으로 어떻게 될 가능성이 있는가

=== 소제목(H2/H3) 규칙 ===

{heading_style}

절대 금지: "왜 중요한가" / "개요" / "결론" / "전망" / "핵심 요약" 같은 추상적 소제목.
해당 섹션의 핵심 키워드가 담긴 구체적 문장형으로 작성.

=== 어조 ===

- 직접 써본 블로거처럼: "직접 써보니", "실제로 적용해보면", "생각보다", "의외로", "가장 놀랐던 점은"
- 강의·설명 금지. 관찰과 경험 공유 방식으로.
- 뉴스 기자 말투 금지 (~했습니다, ~밝혔습니다 반복).

=== 금지 표현 ===

살펴보겠습니다 / 알아보겠습니다 / 소개해드립니다 / 전달해드립니다 /
안녕하세요 / 오늘은 ~ 알아보겠습니다 / 도움이 되길 바랍니다

=== 콘텐츠 흐름 ===

1. <p> 첫 문단 — 독자 관심사 직접 시작, 추상적 사회 이야기 금지
2. H2 섹션 3~4개
3. 핵심 수치 → stat-grid
4. 핵심 포인트 → point-box
5. 개인 의견/경험 → my-opinion
6. 행동 가이드 → checklist (투자·사용 관련이면 필수)
7. 마무리 → conclusion-box
8. FAQ 3개 이상 (실제 검색어 형태)

=== HTML 규칙 ===

content에 <h1> 금지.
허용 태그: <h2> <h3> <p> <table> <thead> <tbody> <tr> <th> <td> <ul> <li> <mark>
허용 클래스: stat-grid, stat-item, point-box, my-opinion, checklist, conclusion-box, quote-box
독자가 반드시 기억해야 할 핵심 문구(2~5단어)는 <mark>문구</mark>로 감싸라. 문단당 최대 2개.
FAQ: <div class="faq-item"><h3 class="faq-q">Q: 질문</h3><p class="faq-a">A: 답변</p></div>

=== 길이 === 1500~3000자

=== 핵심 요약 ===
독자가 바로 알아야 할 핵심 포인트 3가지를 15~30자 문장으로. 팩트 기반.

반드시 JSON만 출력:
{{
  "keyword": "{keyword}",
  "title": "25자 이내, 위 패턴 중 하나, 핵심 키워드 포함",
  "meta_description": "100~160자",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "summary": ["핵심 포인트 1 (15~30자)", "핵심 포인트 2", "핵심 포인트 3"],
  "content": "WordPress HTML 본문"
}}
"""


async def generate_kr_guide_post(
    used_keywords: list[str] | None = None,
    exclude_categories: set[str] | None = None,
    *,
    forced_keyword: str | None = None,
    forced_category: str | None = None,
) -> dict:
    """한국어 검색형 가이드 포스트 생성.
    forced_keyword/forced_category가 있으면 topic engine 결과를 우선 사용.
    없으면 기존 고정 풀에서 폴백 선택.
    """
    from datetime import date
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    today = date.today().strftime("%Y년 %m월 %d일")
    heading_style = random.choice(_HEADING_STYLES)

    if forced_keyword and forced_category:
        keyword = forced_keyword
        category = forced_category
    else:
        pool = _KR_GUIDE_KEYWORDS
        if used_keywords:
            pool = [k for k in pool if k["keyword"] not in used_keywords] or pool
        if exclude_categories:
            filtered = [k for k in pool if k["category"] not in exclude_categories]
            pool = filtered or pool
        chosen = random.choice(pool)
        keyword = chosen["keyword"]
        category = chosen["category"]

    client = genai.Client(api_key=api_key)
    prompt = _KR_GUIDE_PROMPT.format(
        keyword=keyword, category=category, today=today, heading_style=heading_style,
    )
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.85,
            max_output_tokens=8192,
        ),
    )

    try:
        data = _parse_json_safe(response.text)
    except json.JSONDecodeError as e:
        logger.error("KR guide JSON parse failed: %s\nRaw: %s", e, response.text[:500])
        raise RuntimeError(f"KR guide 파싱 실패: {e}")

    required = {"keyword", "title", "meta_description", "tags", "content"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError(f"KR guide 필드 누락: {missing}")

    data.setdefault("summary", [])
    data["_category"] = category
    data["content"] = _fix_ai_headers(data["content"])
    logger.info(
        "KR guide post generated — keyword='%s', title='%s', chars=%d",
        keyword, data["title"], len(data["content"]),
    )
    return data


_EN_SLOT_LABELS = {
    "morning": "Morning (AI & Tech)",
    "afternoon": "Afternoon (AI & Markets)",
    "evening": "Evening (AI & Market Analysis)",
}

_EN_HEADING_STYLES = [
    "Question-form: Each H2/H3 as a real question readers would Google. E.g. 'Is Claude Code Better Than Cursor?' / 'How Does This Affect My Portfolio?'",
    "Summary-form: Each H2/H3 as a keyword-rich section summary. E.g. 'Nvidia Q3 Earnings: What the Numbers Actually Mean' / 'Why Claude Code Is Replacing Traditional Dev Workflows'",
    "Statement-form: Each H2/H3 as a bold claim or key insight. E.g. '3 Things to Know Before Buying AI Stocks' / 'The Real Reason Bitcoin Is Rallying Again'",
]

_ENGLISH_ANALYTICAL_PROMPT = """
You are an expert content writer for an English-language blog targeting global readers interested in AI tools, automation, and investing.

Category: {category}
Publish time: {slot_label}
Date: {today}

=== NEWS CONTEXT ===
{news_text}
=== END NEWS ===

Write an ORIGINAL English blog post — not a translation. Think about what English speakers actually search for on this topic.

=== HEADING STYLE ===
{heading_style}

Forbidden headings: "Overview", "Introduction", "Conclusion", "Key Takeaways", "What Is...", "Why It Matters", "Final Thoughts", "Summary"
→ Use specific, keyword-rich headings that reflect the actual section content.

=== CONTENT FLOW ===
1. <p> Opening — hook immediately, no abstract intros
2. H2 sections (3-4), following heading style above
3. Key stats → stat-grid:
   <div class="stat-grid"><div class="stat-item"><h3>Number</h3><p>Label</p></div></div>
4. Core insight → point-box:
   <div class="point-box">💡 One key sentence</div>
5. Personal take → my-opinion:
   <div class="my-opinion"><h3>✍️ My Take</h3><p>...</p></div>
6. Action items (if relevant) → checklist:
   <div class="checklist"><h3>✅ What to Watch</h3><ul><li>...</li></ul></div>
7. Wrap up → conclusion-box:
   <div class="conclusion-box"><h3>Bottom Line</h3><p>...</p></div>
8. FAQ — 3+ real questions English speakers Google (required)

=== TONE ===
- Like a knowledgeable friend sharing notes — NOT a journalist or professor
- First-person observations: "I've been watching...", "What struck me...", "Turns out..."
- Vary sentence length. Short punchy sentences work.
- NO: "In today's fast-paced world", "Let's dive in", "As we can see", "It goes without saying"
- YES: "Turns out...", "Worth noting...", "Here's the thing...", "What I found..."

=== FORBIDDEN PHRASES ===
- "In today's rapidly evolving landscape"
- "Let me walk you through"
- "In conclusion" / "To summarize"
- "It is important to note"
- "As mentioned above"

=== FACTUAL ACCURACY ===
Only state facts from the news context. For uncertain info: "reportedly", "according to reports".
Do NOT invent statistics or quotes not present in the news.

=== HTML RULES ===
No <h1> in content. No <a> tags — absolutely no links or URLs in the content.
Allowed tags: <h2> <h3> <p> <table> <thead> <tbody> <tr> <th> <td> <ul> <li> <mark>
Allowed classes: stat-grid, stat-item, point-box, my-opinion, checklist, conclusion-box, quote-box
Highlight key phrases (2-5 words) readers MUST remember: <mark>phrase</mark> (max 2 per paragraph). Use <mark> instead of <strong>.
FAQ: <div class="faq-item"><h3 class="faq-q">Q: question</h3><p class="faq-a">A: answer</p></div>

=== LENGTH ===
1500-3000 characters

=== SUMMARY ===
3 bullet points readers need immediately (15-30 words each). Fact-based, include specific numbers where available.

Output JSON only:
{{
  "keyword": "exact phrase English speakers Google (e.g. 'Claude Code automation guide')",
  "title": "Under 60 chars — real search form, includes keyword, no clickbait, no dates",
  "meta_description": "150-160 characters",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "summary": ["Key point 1 (15-30 words)", "Key point 2", "Key point 3"],
  "content": "WordPress HTML body"
}}
"""


async def generate_english_post(
    news_items: list[dict],
    slot: str,
    category: str,
    keyword: str = "",
) -> dict:
    """뉴스 기반 영어 포스트 생성 (한국어 포스트와 asyncio.gather로 동시 생성)."""
    from datetime import date
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    today = date.today().strftime("%B %d, %Y")
    slot_label = _EN_SLOT_LABELS.get(slot, slot)
    heading_style = random.choice(_EN_HEADING_STYLES)

    if news_items:
        news_text = ""
        for i, item in enumerate(news_items[:10], 1):
            news_text += f"[Article {i}] {item.get('title', '')}\n"
            if item.get("description"):
                news_text += f"Summary: {item['description']}\n"
            news_text += "\n"
    else:
        topic = keyword or category
        news_text = f"Topic: {topic}\n(No specific news — write a practical evergreen guide based on your knowledge.)"

    client = genai.Client(api_key=api_key)
    prompt = _ENGLISH_ANALYTICAL_PROMPT.format(
        category=category,
        slot_label=slot_label,
        today=today,
        news_text=news_text,
        heading_style=heading_style,
    )

    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.8,
            max_output_tokens=8192,
        ),
    )

    try:
        data = _parse_json_safe(response.text)
    except json.JSONDecodeError as e:
        logger.error("English post JSON parse failed: %s\nRaw: %s", e, response.text[:500])
        raise RuntimeError(f"English post 파싱 실패: {e}")

    required = {"keyword", "title", "meta_description", "tags", "content"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError(f"English post 필드 누락: {missing}")

    data.setdefault("summary", [])
    logger.info(
        "English post generated — slot=%s, style=%s, keyword='%s', title='%s', chars=%d",
        slot, heading_style[:15], data["keyword"], data["title"], len(data["content"]),
    )
    return data


_EN_GUIDE_KEYWORDS: list[dict] = [
    {"keyword": "Claude Code automation guide",       "category": "Automation"},
    {"keyword": "best MCP servers for developers",    "category": "Guides"},
    {"keyword": "Claude Code vs Cursor AI",           "category": "AI"},
    {"keyword": "how to use Gemini API free",         "category": "Guides"},
    {"keyword": "AI workflow automation tools 2025",  "category": "Automation"},
    {"keyword": "Cursor AI review for beginners",     "category": "AI"},
    {"keyword": "how to build AI agent with Python",  "category": "Automation"},
    {"keyword": "best AI coding assistants ranked",   "category": "AI"},
    {"keyword": "Claude API vs OpenAI API comparison","category": "AI"},
    {"keyword": "MCP server setup tutorial",          "category": "Guides"},
    {"keyword": "AI automation examples for work",    "category": "Automation"},
    {"keyword": "how to use Claude Code effectively", "category": "Guides"},
    {"keyword": "Gemini 2.5 Flash vs GPT-4o speed",  "category": "AI"},
    {"keyword": "n8n AI workflow tutorial",           "category": "Automation"},
    {"keyword": "prompt engineering best practices",  "category": "Guides"},
    {"keyword": "Claude vs ChatGPT for coding",       "category": "AI"},
    {"keyword": "how to automate blog posts with AI", "category": "Automation"},
    {"keyword": "AI tools for content creators 2025", "category": "Guides"},
    {"keyword": "local LLM setup guide Ollama",       "category": "Guides"},
    {"keyword": "AI investment tools and screeners",  "category": "Markets"},
]

_EN_GUIDE_PROMPT = """
You are an expert English-language blogger writing practical How-To content for developers, investors, and tech enthusiasts.

Topic keyword: {keyword}
Category: {category}
Date: {today}

Write a PRACTICAL, ACTIONABLE English guide — not a news article. Readers are searching for step-by-step advice, comparisons, or real-world examples.

=== HEADING STYLE ===
{heading_style}

Forbidden headings: "Introduction", "Overview", "Conclusion", "Summary", "What Is...", "Final Thoughts"
→ Use specific, concrete headings. Include the keyword or a related term.

=== CONTENT FLOW ===
1. <p> Hook — state the problem/benefit immediately. No "In today's world..." intros.
2. H2 sections (3-4): practical steps, comparisons, real examples
3. Key stats or specs → stat-grid (if applicable):
   <div class="stat-grid"><div class="stat-item"><h3>Value</h3><p>Label</p></div></div>
4. Core tip → point-box:
   <div class="point-box">💡 One actionable insight</div>
5. Personal take or common mistake → my-opinion:
   <div class="my-opinion"><h3>✍️ My Take</h3><p>...</p></div>
6. Checklist or quick-start steps (required):
   <div class="checklist"><h3>✅ Quick Checklist</h3><ul><li>...</li></ul></div>
7. Wrap-up → conclusion-box:
   <div class="conclusion-box"><h3>Bottom Line</h3><p>...</p></div>
8. FAQ — 3+ questions English speakers actually Google about this topic (required)

=== TONE ===
- Practical, direct, like a senior dev sharing notes
- Use "you", active voice, short sentences
- First-person observations welcome: "What I found...", "Here's the thing...", "Turns out..."
- NO: "In today's rapidly evolving landscape", "Let's dive in", "It's worth noting that", "As we can see"

=== HTML RULES ===
No <h1> in content. No <a> tags — absolutely no links or URLs in the content.
Allowed tags: <h2> <h3> <p> <table> <thead> <tbody> <tr> <th> <td> <ul> <li> <mark>
Allowed classes: stat-grid, stat-item, point-box, my-opinion, checklist, conclusion-box, quote-box
Highlight key phrases (2-5 words) readers MUST remember: <mark>phrase</mark> (max 2 per paragraph). Use <mark> instead of <strong>.
FAQ: <div class="faq-item"><h3 class="faq-q">Q: question</h3><p class="faq-a">A: answer</p></div>

=== LENGTH ===
1500-3000 characters

=== SUMMARY ===
3 bullet points — key takeaways a reader should remember (15-30 words each). Actionable.

Output JSON only:
{{
  "keyword": "{keyword}",
  "title": "Under 60 chars — exact search form, includes keyword, no clickbait, no dates",
  "meta_description": "150-160 characters",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "summary": ["Takeaway 1 (15-30 words)", "Takeaway 2", "Takeaway 3"],
  "content": "WordPress HTML body"
}}
"""


async def generate_english_guide_post(
    used_keywords: list[str] | None = None,
    *,
    forced_keyword: str | None = None,
    forced_category: str | None = None,
) -> dict:
    """영어 How-To 가이드 포스트 생성.
    forced_keyword/forced_category가 있으면 topic engine 결과를 우선 사용.
    없으면 기존 고정 풀에서 폴백 선택.
    """
    from datetime import date
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    today = date.today().strftime("%B %d, %Y")
    heading_style = random.choice(_EN_HEADING_STYLES)

    if forced_keyword and forced_category:
        keyword = forced_keyword
        category = forced_category
    else:
        pool = _EN_GUIDE_KEYWORDS
        if used_keywords:
            pool = [k for k in pool if k["keyword"] not in used_keywords] or _EN_GUIDE_KEYWORDS
        chosen = random.choice(pool)
        keyword = chosen["keyword"]
        category = chosen["category"]

    client = genai.Client(api_key=api_key)
    prompt = _EN_GUIDE_PROMPT.format(
        keyword=keyword, category=category, today=today, heading_style=heading_style,
    )
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.85,
            max_output_tokens=8192,
        ),
    )

    try:
        data = _parse_json_safe(response.text)
    except json.JSONDecodeError as e:
        logger.error("EN guide JSON parse failed: %s\nRaw: %s", e, response.text[:500])
        raise RuntimeError(f"EN guide 파싱 실패: {e}")

    required = {"keyword", "title", "meta_description", "tags", "content"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError(f"EN guide 필드 누락: {missing}")

    data.setdefault("summary", [])
    data["_category"] = category
    logger.info(
        "EN guide post generated — keyword='%s', title='%s', chars=%d",
        keyword, data["title"], len(data["content"]),
    )
    return data


# ── EN Hub-and-Spoke 프롬프트 (SEO 리팩토링, run_en_guide_pipeline 전용) ──────
# 위의 _EN_GUIDE_PROMPT/generate_english_guide_post()는 구 경로 — 건드리지 않음.

_EN_INTENT_GUIDE: dict[str, str] = {
    "Beginner": (
        "Structure: assume zero prior knowledge, define jargon on first use, walk through the "
        "very first actions a newcomer takes. Title pattern: 'How to' / 'What is' / 'Getting Started'."
    ),
    "Tutorial": (
        "Structure: exactly this shape — H2 'What You'll Need' (brief, skip if trivial), "
        "H2 'Step-by-Step' (numbered steps, 3-6, each a concrete command/click path/setting name), "
        "H2 'A Few More Tips', H2 FAQ. Title pattern: 'How to' / 'Tutorial' / 'Checklist'."
    ),
    "Comparison": (
        "Structure: a head-to-head <table> comparing the two tools on 4-6 concrete axes (price, "
        "speed, features, learning curve), THEN a verdict section for 'who should pick which'. "
        "Do not just describe both separately — compare directly. Title pattern: '{A} vs {B}' / 'Comparison'."
    ),
    "Pricing": (
        "Structure: break down every pricing tier with real numbers, what's included at each "
        "level, and a verdict on value for different user types. Title pattern: 'Pricing' / "
        "'Is It Worth It'."
    ),
    "Troubleshooting": (
        "Structure: exactly this shape — H2 'The Problem' (describe the exact symptom concretely, "
        "as the reader is experiencing it right now), H2 'Why This Happens' (real causes, ranked "
        "by how common they are), H2 'How to Fix It' (numbered fixes, most likely first), "
        "H2 'A Few More Tips', H2 FAQ. Assume the reader is mid-troubleshooting and wants the fix "
        "fast. Title pattern: state the exact symptom as a question, then 'How to Fix' / 'Mistakes' — "
        "never lead the title with the bare label word 'Troubleshooting'."
    ),
    "Review": (
        "Structure: opinionated but fact-grounded — what's genuinely good, what's not, a clear "
        "verdict. Title pattern: 'Review' / 'Pros & Cons'."
    ),
    "Alternatives": (
        "Structure: list format, brief concrete evaluation of each alternative, not just a list "
        "of names. Title pattern: 'Alternatives' / 'Top' / 'Best'."
    ),
    "News": (
        "Structure: what happened, why it matters, what's likely next. Only state facts actually "
        "known — do not invent details. Title pattern: 'Why' / 'What is'."
    ),
    "API": (
        "Structure: technical audience — endpoints, auth, rate limits, pricing, a real "
        "code/request example. Title pattern: 'API' / 'Tutorial'."
    ),
    "Best Practices": (
        "Structure: concrete do's and don'ts, each with a one-line reason why it matters. "
        "Title pattern: 'Best Practices' / 'Tips' / 'Checklist'."
    ),
    "Tips": (
        "Structure: quick actionable list, minimal explanation needed per item. "
        "Title pattern: 'Tips' / 'Best'."
    ),
    "Examples": (
        "Structure: concrete real-world use cases/showcases, specific not generic. "
        "Title pattern: 'Examples' / 'Case Study'."
    ),
}
_EN_DEFAULT_INTENT_GUIDE = _EN_INTENT_GUIDE["Tips"]

_EN_OPENER_PATTERNS = """
OPENING PATTERNS (start the first <p> with one of these):

Pattern A — the problem question (DEFAULT — use this whenever the topic has any how-to,
fix, or "is X worth it" angle): open with the exact question the reader is silently asking,
as a direct question naming the specific problem or decision. This is the primary pattern —
prefer it unless the topic is clearly pure comparison/listicle/news.
  e.g. "Is {hub} not launching?"
  e.g. "Did things slow down after the last {hub} update?"
  e.g. "Is the free version of {hub} actually enough?"

Pattern B — the search reason: Name the exact reason someone is Googling this right now.
  e.g. "If you're comparing {hub} against alternatives before committing your workflow to it, here's what actually matters."

Pattern C — the concrete claim: Open with a specific number, result, or observation, not a topic statement.
  e.g. "It took me 12 minutes to get {hub} running — most of that was one step nobody documents well."

Do NOT open with a definition, a generic statement about the industry, or "In this article/guide/post".
"""

# 단일 소스 — blog_topic_engine.py의 title_seed 생성 단계와 동일한 규칙을 최종 제목
# 폴리싱 단계에서도 쓴다. 두 곳에 따로 유지하면 한쪽만 고쳤을 때 서로 어긋나
# (예: 한쪽엔 금지했는데 다른 쪽엔 여전히 예시로 남아있는 등) 금지어가 새 나가는 문제가 생긴다.
from services.blog_topic_engine import _EN_BANNED_TITLE_WORDS, _EN_ALLOWED_TITLE_PATTERNS

_EN_HUB_PROMPT = """
You are an expert English-language tech blogger writing for kekegroup.uk, a site built on
topical authority — deep, specific coverage of a small set of tools, not shallow breadth.

Hub (the tool/company this post belongs to): {hub}
Primary keyword / subtopic angle: {keyword}
Search intent: {intent}
Category: {category}
Date: {today}

=== SEARCH INTENT ===
{intent_guide}

=== TITLE RULES ===
NEVER use these words in the title, anywhere: {banned_words}
ONLY use one of these title patterns: {allowed_patterns}
Title must be under 60 characters, must contain "{hub}", no clickbait, no dates, no colon-then-subtitle padding.
Using a pattern word as the title's own grammar is correct and required (e.g. "What is MCP?",
"Claude vs ChatGPT" — these are fine, do NOT change this). The only thing to avoid is bolting the
pattern name on AGAIN as a separate trailing tag after the title already reads as a complete sentence
on its own — that's redundant, not an extra pattern usage.
  Bad:  "Claude Code vs GitHub Copilot: Comparison"  (already says "vs", then tags "Comparison" again)
  Good: "Claude Code vs GitHub Copilot for Developers"
  Bad:  "ChatGPT Pricing: Pricing"  (says "Pricing" twice)
  Good: "ChatGPT Pricing Explained (With Real Numbers)"
  Fine as-is: "What is MCP for Developers?" (pattern used once, naturally — no fix needed)

Write the title as a real sentence a person would search, not a category label with the hub
name bolted on:
  Bad:  "Troubleshooting {hub} Crashing"  Good: "{hub} Keeps Crashing? Here's How to Fix It"
  Bad:  "{hub} Review: AI Productivity Verdict"  Good: "{hub} Review: Is It Worth Paying for in 2026?"
Avoid vague abstract phrases entirely — they don't tell the reader what they'll learn:
  "Who Benefits?" -> say who, specifically (e.g. "Which Is Better for Developers?")
  "Your Workflow" / "Verdict" / "The Future of X" / "A New Era of X" -> state the real question
  or outcome instead.
Before finalizing, check: if a real searcher clicked this title, could they immediately say what
they're about to learn? If not, rewrite it.

=== HEADING STYLE ===
{heading_style}
Forbidden headings: "Introduction", "Overview", "Conclusion", "Summary", "What Is...", "Final Thoughts"

=== OPENING ===
{opener_patterns}

=== CONTENT FLOW ===
1. <p> Opening — use one of the patterns above. No throat-clearing.
2. H2 sections (3-5) following the intent structure above.
3. Key stats/specs → stat-grid (if applicable):
   <div class="stat-grid"><div class="stat-item"><h3>Value</h3><p>Label</p></div></div>
4. Core tip → point-box:
   <div class="point-box">💡 One actionable insight</div>
5. Personal take or common mistake → my-opinion:
   <div class="my-opinion"><h3>✍️ My Take</h3><p>...</p></div>
6. Checklist or quick-start steps (required):
   <div class="checklist"><h3>✅ Quick Checklist</h3><ul><li>...</li></ul></div>
7. Wrap-up → conclusion-box:
   <div class="conclusion-box"><h3>Bottom Line</h3><p>...</p></div>
8. FAQ — 3+ real questions people Google about "{hub} {keyword_short}" (required)

=== TONE ===
- Practical, direct, like a senior dev/analyst sharing notes — not a journalist or professor
- Use "you", active voice, short sentences. First-person: "What I found...", "Here's the thing...", "Turns out..."
- NO: "In today's rapidly evolving landscape", "In this guide", "In today's article", "Let's dive in",
  "This comprehensive guide", "It's worth noting that", "As we can see", "Let me walk you through"

=== HTML RULES ===
No <h1> in content. No <a> tags — absolutely no links or URLs in the content.
Allowed tags: <h2> <h3> <p> <table> <thead> <tbody> <tr> <th> <td> <ul> <li> <mark>
Allowed classes: stat-grid, stat-item, point-box, my-opinion, checklist, conclusion-box, quote-box
Highlight key phrases (2-5 words) with <mark> instead of <strong> (max 2 per paragraph).
FAQ: <div class="faq-item"><h3 class="faq-q">Q: question</h3><p class="faq-a">A: answer</p></div>

=== LENGTH ===
1500-3000 characters

=== SUMMARY ===
3 bullet points — key takeaways (15-30 words each). Actionable.

Output JSON only:
{{
  "title": "Under 60 chars, contains '{hub}', follows the allowed title patterns above",
  "meta_description": "150-160 characters",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "summary": ["Takeaway 1 (15-30 words)", "Takeaway 2", "Takeaway 3"],
  "content": "WordPress HTML body"
}}
"""


async def generate_english_hub_post(keyword: str, category: str, hub: str, intent: str) -> dict:
    """Hub-and-Spoke EN 포스트 생성. generate_english_guide_post()와 별개 — 구 경로 미변경."""
    from datetime import date
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    today = date.today().strftime("%B %d, %Y")
    heading_style = random.choice(_EN_HEADING_STYLES)
    keyword_short = keyword.replace(hub, "").strip()

    client = genai.Client(api_key=api_key)
    prompt = _EN_HUB_PROMPT.format(
        hub=hub, keyword=keyword, keyword_short=keyword_short, intent=intent, category=category,
        today=today, heading_style=heading_style,
        intent_guide=_EN_INTENT_GUIDE.get(intent, _EN_DEFAULT_INTENT_GUIDE),
        opener_patterns=_EN_OPENER_PATTERNS.format(hub=hub),
        banned_words=", ".join(_EN_BANNED_TITLE_WORDS),
        allowed_patterns=", ".join(_EN_ALLOWED_TITLE_PATTERNS),
    )
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.85,
            max_output_tokens=8192,
        ),
    )
    try:
        data = _parse_json_safe(response.text)
    except json.JSONDecodeError as e:
        logger.error("EN hub post JSON parse failed: %s\nRaw: %s", e, response.text[:500])
        raise RuntimeError(f"EN hub post 파싱 실패: {e}")

    required = {"title", "meta_description", "tags", "content"}
    missing = required - data.keys()
    if missing:
        raise RuntimeError(f"EN hub post 필드 누락: {missing}")
    data.setdefault("summary", [])
    logger.info(
        "EN hub post generated — hub=%s, title='%s', chars=%d",
        hub, data["title"], len(data["content"]),
    )
    return data


# ── 품질 검증 게이트 (EN hub 파이프라인 전용) ─────────────────────────────────

_QUALITY_AXES = ["seo_title", "predicted_ctr", "intent_fit", "human_sounding", "naturalness"]

_QUALITY_PROMPT = """
You are a strict SEO/content quality auditor for a tech blog built on topical authority.
Score the post below on 5 axes, 0-100 each (100 = best):

1. seo_title — Title matches real search intent, avoids banned words ({banned_words}),
   uses an allowed CTR pattern ({allowed_patterns}), under 60 characters. A title that uses a
   pattern word once, naturally, as its own grammar (e.g. "What is MCP?", "Claude vs ChatGPT")
   is CORRECT — do not penalize that. Only penalize titles that state the pattern TWICE — once
   naturally plus once again as a bolted-on trailing tag (e.g. "X vs Y: Comparison" says "vs"
   then redundantly tags "Comparison"; "X: Explained" repeats "Explained" as dead weight).
2. predicted_ctr — Would a real searcher click this over competing results?
3. intent_fit — Does the article's structure and title match what a real searcher with
   intent "{intent}" for keyword "{keyword}" actually expects to find? (e.g. Tutorial/Beginner
   needs real numbered steps; Comparison needs a direct side-by-side; Troubleshooting needs
   cause→fix; Pricing needs real numbers; Review needs a clear verdict.)
4. human_sounding — Higher = sounds like a real person. Lower = generic AI buzzwords, empty
   transitions, no concrete specifics/numbers/opinions.
5. naturalness — Sentence rhythm, tone consistency, no robotic/repeated phrasing.

Hub: {hub}
Primary keyword: {keyword}
Search intent: {intent}
Title: {title}
Meta description: {meta_description}
Content (HTML stripped):
{content}

Output JSON only:
{{"seo_title":0,"predicted_ctr":0,"intent_fit":0,"human_sounding":0,"naturalness":0,
  "average":0,"reason":"single biggest weakness, or 'none'"}}
"""


async def score_post_quality(keyword: str, intent: str, hub: str, title: str,
                              meta_description: str, content: str) -> dict:
    """2차 Gemini 호출로 5축 품질 평가. 채점 자체가 실패하면 관대한 통과 처리(비차단) —
    콘텐츠가 나쁜 게 아니라 채점기가 죽은 것 때문에 재생성을 강제해 API 비용을 낭비하지 않기 위함."""
    try:
        client = _make_client()
        content_sample = re.sub(r"<[^>]+>", " ", content)[:4000]
        prompt = _QUALITY_PROMPT.format(
            hub=hub, keyword=keyword, intent=intent, title=title,
            meta_description=meta_description, content=content_sample,
            banned_words=", ".join(_EN_BANNED_TITLE_WORDS),
            allowed_patterns=", ".join(_EN_ALLOWED_TITLE_PATTERNS),
        )
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=1024,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (response.text or "").strip())
        data = json.loads(raw)
        scores = {k: int(data.get(k, 0)) for k in _QUALITY_AXES}
        avg = data.get("average")
        avg = float(avg) if isinstance(avg, (int, float)) else sum(scores.values()) / len(scores)
        return {**scores, "average": avg, "reason": data.get("reason", "")}
    except Exception as e:
        logger.warning("Quality gate scoring failed (treating as pass): %s", e)
        return {**{k: 100 for k in _QUALITY_AXES}, "average": 100.0, "reason": f"scoring unavailable: {e}"}


async def generate_post_image(keyword: str, title: str, category: str = "AI", summary: list[str] | None = None, hub: str | None = None) -> tuple[bytes, str] | None:
    """Pexels API로 관련 스톡 이미지 검색 후 다운로드. 실패 시 None 반환."""
    pexels_key = os.getenv("PEXELS_API_KEY", "")
    if not pexels_key:
        logger.debug("PEXELS_API_KEY not set, skipping image")
        return None
    try:
        import httpx
        en_query = await _generate_image_search_query(keyword, title, category, summary=summary, hub=hub)
        async with httpx.AsyncClient(timeout=15) as http:
            search_r = await http.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": pexels_key},
                params={"query": en_query, "per_page": 15, "orientation": "landscape"},
            )
            search_r.raise_for_status()
            photos = search_r.json().get("photos", [])
            if not photos:
                return None
            photo = random.choice(photos)
            img_url = photo["src"]["large2x"]
            img_r = await http.get(img_url)
            img_r.raise_for_status()
            logger.info("Pexels photo selected: id=%s", photo["id"])
            return img_r.content, "image/jpeg"
    except Exception as e:
        logger.warning("Pexels image fetch failed (skipping): %s", e)
    return None
