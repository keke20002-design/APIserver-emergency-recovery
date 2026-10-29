"""TrendLab Fact Extractor — 사실/발언/의견 분리, 논란의 핵심 추출, 현재 확인된 내용 검증.

analyzer.py가 이미 같은 사건으로 묶은 source_items(뉴스+유튜브 원문)를 입력받아,
글 생성 전에 이 사건에 대해 무엇이 사실이고 무엇이 아직 발언/미확인 상태인지를
Gemini로 먼저 구조화한다. 생성 단계(issue_generator)는 이 구조화된 결과만 근거로
글을 쓰기 때문에, 매번 원문을 다시 읽고 자유롭게 서술하며 사실과 발언을 섞는 것을 막는다.

"온라인에서 제기된 주장"처럼 발언자 없는 익명 claims는 신뢰도를 떨어뜨리므로 쓰지 않는다.
대신 누가 말했는지(speaker)를 반드시 명시하는 statements(주장/반박/보도)로 구조화한다.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# 정치/사회 카테고리에서만: 양측 대립 구도(sides) 렌더링, "아직 확인되지 않은 부분" 재도입,
# 정치적 추측 금지 규칙, 출처 표기를 적용한다. 그 외 카테고리는 지금까지와 동일하게 둔다.
POLITICAL_SOCIAL_CATEGORIES = {"정치", "사회"}

_STATEMENT_TYPES = {"주장", "반박", "보도"}


def _format_pub_date(published_at: str) -> str:
    """ISO 타임스탬프(2026-09-15T15:27:17+00:00) → 모델이 그대로 베껴도 안전한
    'YYYY-MM-DD' 형태로만 축약. 본문에 원문 그대로 노출되면 괄호 출처 표기처럼
    보여 가독성을 해치므로, 프롬프트에 넣는 시점부터 시각/타임존을 없앤다."""
    if not published_at:
        return "날짜 미상"
    return published_at.split("T", 1)[0]


def _build_source_block(source_items: list[dict], max_items: int = 15) -> str:
    """source_items(같은 사건으로 묶인 원문 기사/영상 목록) → 프롬프트용 텍스트."""
    def _key(i: dict) -> str:
        return i.get("published_at") or ""

    items = sorted(source_items, key=lambda i: (bool(i.get('article_text')), _key(i)), reverse=True)[:max_items]
    lines = []
    for idx, i in enumerate(items, 1):
        src = "뉴스" if i.get("source") == "naver" else "유튜브"
        publisher = i.get("publisher", "")
        title = i.get("title", "")
        desc = i.get("description", "")
        pub = _format_pub_date(i.get("published_at") or "")
        tag = f"{src}·{publisher}" if publisher else src
        lines.append(f"[{idx}][{tag}] ({pub}) {title}")
        if desc:
            lines.append(f"  → {desc}")
    return "\n".join(lines) if lines else "(원문 소스 없음)"


_EMPTY_ANALYSIS = {
    "facts": [], "statements": [], "unconfirmed": [], "sides": [],
    "controversy_core": "", "why_trending": "", "current_reaction": "",
    "qa": [], "entities": [], "background": [], "timeline": [],
    "has_controversy": False,
}

_ANALYZE_PROMPT = """
아래는 '{topic}' 사건에 대해 같은 사건으로 묶인 뉴스/유튜브 원문 목록이다 (카테고리: {category}).

{source_block}

이 원문들만 근거로 다음을 분석하라. 원문에 없는 내용은 절대 추가하지 마라.

1. facts (확인된 사실): 복수 매체가 동일하게 보도했거나, 당사자·공식 발표·1차 자료(경찰/검찰/정부 등
   공식 기관의 발표 포함)로 확인된 내용만. 아직 한 매체만 보도했거나 추측성인 내용은 여기 넣지 마라.
   각 항목에는 가능하면 "언제(날짜/시점)"를 포함해서 써라 (예: "8월 26일 경찰이 체포 사실을 공개했다").
   확인되지 않은 내용을 "실제로 ~인 것으로 확인됐다"처럼 단정해서 facts에 넣지 마라.
   여러 세부사항을 한 항목에 뭉뚱그리지 말고, 원문에 있는 만큼 세분화해서 각각 별도 항목으로 써라
   (예: 발표일, 제목/명칭, 형식·구성, 공개 방식, 구체적 수치 등을 각각 나눠서). 원문에 실제로 있는
   세부사실이면 개수를 인위적으로 줄이지 마라 — 정보가 많으면 많은 대로 다 담는다. 단, 같은 사실을
   표현만 바꿔 두 번 쓰지 마라 — 이미 다른 항목에 담은 내용이면 새 항목을 만들지 마라.
   각 항목: {{"fact": "사실 내용", "importance": 0~100, "source_id": "이 사실이 나온 원문 소스의 번호
   (아래 원문 소스의 [번호]와 동일)"}}. importance 기준 — 90~100: 기사의 핵심 내용(제목에 들어갈
   만한 사건 자체), 70~89: 중요한 세부 내용, 50~69: 보조 정보, 0~49: 생략해도 무방한 정보. 원문에서
   어느 소스 번호로 확인했는지 알 수 없으면 source_id는 빈 문자열로 둬라(지어내지 마라).

2. statements (발언/반박/보도 — 발언자를 반드시 명시): "온라인에서 주장이 나온다"처럼 발언자 없는
   익명 주장은 절대 만들지 마라. 실제로 특정 인물·매체가 한 말/보도만 담는다. 각 항목:
   {{"speaker": "실명+직함", "type": "주장|반박|보도", "content": "실제 발언/보도 내용", "date": "시점(모르면 빈 문자열)"}}
   - "주장": 당사자 본인 또는 특정 인물이 제기한 의혹/주장 (예: 한동훈 의원 → "~라고 주장했다")
   - "반박": 그 주장에 대해 상대측 인물이 내놓은 반박 (예: 김승원 후보자 → "~라고 반박했다")
   - "보도": 언론사가 보도했지만 아직 사실로 확정되지 않은 내용 (예: "○○ 언론은 ~라고 보도했다")

3. unconfirmed (아직 확인되지 않은 부분): 카테고리가 "정치" 또는 "사회"이고, 기사들 사이에 정보가
   엇갈리거나 원인·경위·당사자 입장 등이 실제로 아직 밝혀지지 않은 지점이 있을 때만 질문 형태로 작성.
   그 외 카테고리이거나 실제로 미확인 지점이 없으면 빈 배열로 둬라.

4. sides (대립하는 양측 입장): 카테고리가 "정치" 또는 "사회"이고, 실제로 두 인물/진영이 서로 다른
   입장(주장 vs 반박)으로 대립하고 있을 때만 작성. 각 항목:
   {{"name": "인물/진영 이름(직함 포함)", "stance": "주장|반박", "points": ["핵심 내용 1", "핵심 내용 2"]}}
   최대 2개 항목. 대립 구도가 명확하지 않거나 원문에 두 입장이 다 없으면 빈 배열.

5. controversy_core: 이 사건에 실제 '논란'(찬반이 갈리거나 이견이 있는 지점)이 있다면 핵심을 한두
   문장으로. 단순 소식 전달이라 논란이랄 게 없으면 빈 문자열로 둬라 — 억지로 만들지 마라.

6. why_trending: 왜 지금 다시 화제/논란이 됐는지 원문에 나온 구체적 계기로 설명 — 예: 청문회, 새로운
   자료 공개, 당사자 발언, 추가 의혹 제기, 수사 진행 상황, 정치권 공방, 사건 발생 시점과 보도 시점의
   시간차 등. "많은 관심을 받고 있다"처럼 뭉뚱그린 설명은 쓰지 마라. 원문에 근거가 없으면 빈 문자열.

7. current_reaction: 현재 반응 경향 요약. 원문에 실제로 인용된 반응/댓글/여론이 있을 때만 작성하고,
   원문에 없는 댓글·반응을 지어내지 마라. 반응 관련 내용이 전혀 없으면 빈 문자열로 둬라.

8. qa (독자가 실제로 궁금해할 질문-답변): "사건은 언제 발생했나?", "언제 알려졌나/체포됐나?",
   "현재 수사·처리는 어디까지 진행됐나?", "당사자의 이력(방송 출연 등)은 확인됐나?"처럼 이 원문에서
   답을 실제로 확인할 수 있는 질문만 답변과 함께 써라. "동기가 무엇인가", "앞으로 어떻게 될 것인가"처럼
   원문에 답이 없는 질문은 만들지 마라. 답할 수 있는 질문이 2개 미만이면 빈 배열로 둬라.

9. entities (관련 인물/작품/요소): 이 사건에 실제로 등장하는 인물·작품·단체·제품 등 중에서, 단순
   언급을 넘어 구체적인 역할·관계가 원문에 설명된 것만 뽑아라 (예: "김영옥: 뮤직비디오에 카메오로
   출연", "유인나: 곡 가사/컨셉과 관련된 인물로 언급됨"). 이름만 나오고 역할·관계 설명이 없으면
   entities에 넣지 마라. 없으면 빈 배열. 각 항목: {{"name": "...", "detail": "...",
   "source_id": "원문 소스 번호(모르면 빈 문자열)"}}.

10. background (배경): 이번 사건이 왜 의미 있는지 설명해주는 과거 이력·관계·이전 활동 등이 원문에
    실제로 있을 때만 써라 (예: "이전 앨범은 1년 전에 발매됐다", "두 사람은 과거 작품에서 함께 작업한
    적이 있다"). 서로 다른 배경 정보가 여러 개 있으면 각각 별도 항목으로 나눠라 — 없는데 억지로
    쪼개지는 마라. 원문에 없는 배경을 추론해서 채우지 마라. 없으면 빈 배열. 각 항목:
    {{"text": "배경 내용", "source_id": "원문 소스 번호(모르면 빈 문자열)"}}.

11. timeline (시간 순서): 원문에 실제로 날짜/시점이 명시된 사건 진행 과정이 있을 때만 시간순으로
    정리한다 (예: "1년 전 이전 앨범 발매", "9월 8일 신곡 발표", "발표 직후 뮤직비디오 공개"). 날짜가
    불명확하거나 순서를 추측해야 하면 만들지 마라 — 원문에 명시된 시점만 담는다. 없으면 빈 배열. 각
    항목: {{"date": "날짜/시점(원문 표현 그대로)", "event": "그 시점에 있었던 일",
    "source_id": "원문 소스 번호(모르면 빈 문자열)"}}.

★★ 절대 금지: statements에 발언자를 명시하지 않는 표현("주장이 나온다", "의혹이 제기된다" 등)을
그대로 넣지 마라 — speaker 필드에 반드시 실명/직함을 채워라. 발언자를 특정할 수 없으면 그 항목 자체를
statements에 넣지 마라.
★★ 절대 금지: 정치적 해석·전망(복당 가능성, 정치적 계산, 선거에 미칠 영향, 정치적 입지 변화, 공천
전망 등)을 원문에 명시된 공식 출처 없이 추가하지 마라.
★★ 절대 금지: 원문이 직접 언급하지 않은 인과관계를 사실처럼 단정하지 마라 (예: 근거 없이
"A 때문에 B가 됐다"라고 쓰지 마라 — 원문이 그 인과관계를 직접 언급한 경우에만 서술 가능하다).
★★ 절대 금지: 실제 댓글·반응을 인용하는 것처럼 꾸며내지 마라 — 원문에 실제로 인용된 문장이 없으면
current_reaction은 경향만 서술(예: "부정적 반응이 우세하다")하고 가짜 대사를 만들지 마라.

출력 JSON만, 설명 금지:
{{"facts": [{{"fact": "...", "importance": 0, "source_id": "..."}}],
"statements": [{{"speaker": "...", "type": "주장|반박|보도", "content": "...", "date": "..."}}],
"unconfirmed": ["..."],
"sides": [{{"name": "...", "stance": "주장|반박", "points": ["..."]}}],
"controversy_core": "...", "why_trending": "...", "current_reaction": "...",
"qa": [{{"question": "...", "answer": "..."}}],
"entities": [{{"name": "...", "detail": "...", "source_id": "..."}}],
"background": [{{"text": "...", "source_id": "..."}}],
"timeline": [{{"date": "...", "event": "...", "source_id": "..."}}]}}
""".strip()


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[가-힣A-Za-z0-9]{2,}", text or ""))


def _dedupe_by_similarity(items: list[dict], text_key: str, threshold: float = 0.7) -> list[dict]:
    """같은 사실/배경/타임라인 항목이 표현만 바꿔 중복 추출된 경우 하나로 합친다.

    "동일 정보를 여러 번 표현한 경우 정보량을 중복 계산하지 않는다"는 요구사항의 코드 안전장치 —
    richness/extraction quality 계산과 실제 프롬프트에 전달되는 데이터 양쪽에 함께 적용된다.
    클러스터 안에서는 importance가 더 높은(또는 없으면 더 긴 서술의) 항목을 대표로 남긴다.

    유사도는 Jaccard(합집합 기준)가 아니라 overlap coefficient(작은 쪽 집합 기준: |A∩B|/min(|A|,|B|))로
    잰다 — "9월 8일 신곡을 발표했다"처럼 한쪽이 다른 쪽을 조사만 바꿔 부분집합처럼 담고 있는 경우가
    실제 근접 중복의 대다수라, 합집합 기준이면 문장 길이 차이 때문에 과소평가되기 쉽다.
    """
    kept: list[dict] = []
    kept_words: list[set[str]] = []
    for item in items:
        words = _word_set(item.get(text_key, ""))
        dup_idx = None
        if words:
            for i, kw in enumerate(kept_words):
                if kw and len(words & kw) / max(1, min(len(words), len(kw))) >= threshold:
                    dup_idx = i
                    break
        if dup_idx is None:
            kept.append(item)
            kept_words.append(words)
            continue
        existing = kept[dup_idx]
        if item.get("importance", 0) > existing.get("importance", 0) or (
            item.get("importance", 0) == existing.get("importance", 0)
            and len(item.get(text_key, "")) > len(existing.get(text_key, ""))
        ):
            kept[dup_idx] = item
    return kept


def _dedupe_entities(entities: list[dict]) -> list[dict]:
    """같은 인물/요소가 이름만 같고 detail 표현이 다르게 두 번 추출된 경우, 더 구체적인(긴) 쪽만 남긴다."""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for e in entities:
        key = e["name"].strip().lower()
        if key not in by_key:
            by_key[key] = e
            order.append(key)
        elif len(e.get("detail", "")) > len(by_key[key].get("detail", "")):
            by_key[key] = e
    return [by_key[k] for k in order]


async def analyze_event(topic: str, category: str, source_items: list[dict]) -> dict:
    """사실/발언 분리 → 논란의 핵심 추출 → 현재 확인된 내용 검증.

    반환:
      facts: [{fact, importance(0~100), source_id}] — importance 내림차순 정렬, 유사 중복 제거됨
      statements: [{speaker, type(주장|반박|보도), content, date}] — 발언자가 명시된 것만
      unconfirmed: 아직 확인되지 않은 부분 (질문 형태) — 정치/사회 카테고리에서만 채워짐
      sides: [{name, stance(주장|반박), points}] — 대립 양측 입장 (정치/사회, 최대 2개)
      controversy_core: 논란의 핵심 (없으면 "")
      why_trending: 왜 화제가 됐는지 (근거 없으면 "")
      current_reaction: 현재 반응 경향 (근거 없으면 "")
      qa: [{question, answer}] — 원문에서 실제 답을 확인할 수 있는 질문만 (2개 미만이면 빈 배열)
      entities: [{name, detail, source_id}] — 구체적 역할이 확인된 관련 인물/작품/요소만
      background: [{text, source_id}] — 과거 이력/맥락 (없으면 빈 배열)
      timeline: [{date, event, source_id}] — 원문에 날짜가 명시된 사건 진행만 (없으면 빈 배열)
      has_controversy: statements/controversy_core 중 하나라도 있으면 True
    """
    if not source_items:
        return dict(_EMPTY_ANALYSIS)

    is_political = category in POLITICAL_SOCIAL_CATEGORIES

    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)

    source_block = _build_source_block(source_items)
    prompt = _ANALYZE_PROMPT.format(topic=topic, category=category, source_block=source_block)

    statement_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "speaker": types.Schema(type=types.Type.STRING),
            "type": types.Schema(type=types.Type.STRING),
            "content": types.Schema(type=types.Type.STRING),
            "date": types.Schema(type=types.Type.STRING),
        },
        required=["speaker", "type", "content"],
    )
    side_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "name": types.Schema(type=types.Type.STRING),
            "stance": types.Schema(type=types.Type.STRING),
            "points": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
        },
        required=["name", "stance", "points"],
    )
    qa_item_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "question": types.Schema(type=types.Type.STRING),
            "answer": types.Schema(type=types.Type.STRING),
        },
        required=["question", "answer"],
    )
    entity_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "name": types.Schema(type=types.Type.STRING),
            "detail": types.Schema(type=types.Type.STRING),
            "source_id": types.Schema(type=types.Type.STRING),
        },
        required=["name", "detail"],
    )
    fact_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "fact": types.Schema(type=types.Type.STRING),
            "importance": types.Schema(type=types.Type.INTEGER),
            "source_id": types.Schema(type=types.Type.STRING),
        },
        required=["fact"],
    )
    background_item_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "text": types.Schema(type=types.Type.STRING),
            "source_id": types.Schema(type=types.Type.STRING),
        },
        required=["text"],
    )
    timeline_item_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "date": types.Schema(type=types.Type.STRING),
            "event": types.Schema(type=types.Type.STRING),
            "source_id": types.Schema(type=types.Type.STRING),
        },
        required=["date", "event"],
    )
    response_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "facts": types.Schema(type=types.Type.ARRAY, items=fact_schema),
            "statements": types.Schema(type=types.Type.ARRAY, items=statement_schema),
            "unconfirmed": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
            "sides": types.Schema(type=types.Type.ARRAY, items=side_schema),
            "controversy_core": types.Schema(type=types.Type.STRING),
            "why_trending": types.Schema(type=types.Type.STRING),
            "current_reaction": types.Schema(type=types.Type.STRING),
            "qa": types.Schema(type=types.Type.ARRAY, items=qa_item_schema),
            "entities": types.Schema(type=types.Type.ARRAY, items=entity_schema),
            "background": types.Schema(type=types.Type.ARRAY, items=background_item_schema),
            "timeline": types.Schema(type=types.Type.ARRAY, items=timeline_item_schema),
        },
        required=[
            "facts", "statements", "unconfirmed", "sides", "controversy_core",
            "why_trending", "current_reaction", "qa", "entities", "background", "timeline",
        ],
    )

    raw = ""
    data: dict = {}
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.2,
                max_output_tokens=8192,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.warning("Fact extraction failed for '%s': %s | raw=%s", topic, e, raw[:200])
        data = {}

    facts = []
    for item in data.get("facts", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("fact", "")).strip()
        if not text:
            continue
        try:
            importance = int(item.get("importance", 50))
        except (TypeError, ValueError):
            importance = 50
        importance = max(0, min(100, importance))
        source_id = str(item.get("source_id", "") or "").strip()
        facts.append({"fact": text, "importance": importance, "source_id": source_id})
    facts = _dedupe_by_similarity(facts, "fact")
    facts.sort(key=lambda f: f["importance"], reverse=True)

    statements = []
    for item in data.get("statements", []):
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker", "")).strip()
        stype = str(item.get("type", "")).strip()
        content = str(item.get("content", "")).strip()
        date = str(item.get("date", "")).strip()
        # 발언자 미상이거나 알 수 없는 타입이면 신뢰도만 떨어뜨리므로 버린다.
        if not speaker or not content or stype not in _STATEMENT_TYPES:
            continue
        statements.append({"speaker": speaker, "type": stype, "content": content, "date": date})

    # unconfirmed/sides는 정치·사회 카테고리에서만 사용 — 모델이 지시를 무시해도 여기서 강제한다.
    if is_political:
        unconfirmed = [u.strip() for u in data.get("unconfirmed", []) if isinstance(u, str) and u.strip()]
        sides = []
        for item in data.get("sides", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            stance = str(item.get("stance", "")).strip()
            points = [p.strip() for p in item.get("points", []) if isinstance(p, str) and p.strip()]
            if not name or stance not in ("주장", "반박") or not points:
                continue
            sides.append({"name": name, "stance": stance, "points": points[:4]})
        sides = sides[:2]
    else:
        unconfirmed = []
        sides = []

    controversy_core = str(data.get("controversy_core") or "").strip()
    why_trending = str(data.get("why_trending") or "").strip()
    current_reaction = str(data.get("current_reaction") or "").strip()
    qa_raw = data.get("qa", [])
    qa = [
        {"question": str(item.get("question", "")).strip(), "answer": str(item.get("answer", "")).strip()}
        for item in qa_raw
        if isinstance(item, dict) and item.get("question") and item.get("answer")
    ]
    # 답변 가능한 질문이 2개 미만이면 FAQ를 만들 근거가 부족한 것 — 생성 단계에서 사용하지 않는다.
    if len(qa) < 2:
        qa = []

    entities = []
    for item in data.get("entities", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        detail = str(item.get("detail", "")).strip()
        source_id = str(item.get("source_id", "") or "").strip()
        # 이름만 있고 구체적인 역할/관계 설명이 없으면 본문에 새 정보를 못 주므로 버린다.
        if not name or not detail:
            continue
        entities.append({"name": name, "detail": detail, "source_id": source_id})
    entities = _dedupe_entities(entities)

    background = []
    for item in data.get("background", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        source_id = str(item.get("source_id", "") or "").strip()
        if not text:
            continue
        background.append({"text": text, "source_id": source_id})
    background = _dedupe_by_similarity(background, "text")

    timeline = []
    for item in data.get("timeline", []):
        if not isinstance(item, dict):
            continue
        tdate = str(item.get("date", "")).strip()
        event = str(item.get("event", "")).strip()
        source_id = str(item.get("source_id", "") or "").strip()
        # 날짜/시점을 특정할 수 없으면 순서를 지어내는 것과 다름없으므로 버린다.
        if not tdate or not event:
            continue
        timeline.append({"date": tdate, "event": event, "source_id": source_id})
    timeline = _dedupe_by_similarity(timeline, "event")

    result = {
        "facts": facts,
        "statements": statements,
        "unconfirmed": unconfirmed,
        "sides": sides,
        "controversy_core": controversy_core,
        "why_trending": why_trending,
        "current_reaction": current_reaction,
        "qa": qa,
        "entities": entities,
        "background": background,
        "timeline": timeline,
        "has_controversy": bool(controversy_core) or bool(statements),
    }
    logger.info(
        "Fact extraction '%s': facts=%d statements=%d unconfirmed=%d sides=%d controversy=%s qa=%d "
        "entities=%d background=%d timeline=%d",
        topic, len(facts), len(statements), len(unconfirmed), len(sides), bool(controversy_core), len(qa),
        len(entities), len(background), len(timeline),
    )
    return result


# 임계값/분량 범위는 실제 보정 예시(🔴~🔵 4단계, 각 단계의 source/facts/entities/background/
# timeline 조합)에 맞춰 역산해 고정한 값이다 — 값을 바꾸면 이 4가지 예시로 다시 검증할 것.
#   🔴 LOW      : source=1, facts=3, entities=1, background=0                     → score≈12  → 600~800자
#   🟡 MEDIUM   : source=4, facts=7, entities=4, background=1, timeline=1         → score≈34  → 1000~1400자
#   🟢 HIGH     : source=8, 독립=5, facts=12, entities=6, background=3, timeline=4, qa=5 → score≈64 → 1400~1900자
#   🔵 VERY_HIGH: source=15, 독립=8, facts=20+, entities=10+, background=5+, timeline=8+, 발언 다수 → score≈105 → 1700~2300자
_LEVEL_RANGES = (
    ("VERY_HIGH", 80.0, 1700, 2300),
    ("HIGH", 45.0, 1400, 1900),
    ("MEDIUM", 15.0, 1000, 1400),
    ("LOW", 0.0, 600, 800),
)


def compute_richness(source_items: list[dict], analysis: dict) -> dict:
    """source richness를 계산해 dynamic article length target을 정한다.

    단순 source 개수가 아니라 서로 다른 매체 수(independent_source_count), 사실/발언/배경/타임라인
    개수, 구체적 역할이 확인된 관련 인물/요소 수(entity_count), 사실의 평균 중요도(importance)를
    함께 반영한다 — 같은 보도자료를 복사한 기사 10개보다, 공식발표+언론보도+배경자료가 섞인 source
    3개가 richness가 더 높게 나올 수 있다. facts/background/timeline은 analyze_event() 단계에서
    이미 유사 중복이 제거된 상태로 들어오므로 여기서 다시 중복 계산할 위험은 없다.
    """
    publishers = {
        (item.get("publisher") or "").strip()
        for item in source_items
        if (item.get("publisher") or "").strip()
    }
    independent_source_count = len(publishers) if publishers else len(source_items)

    facts = analysis.get("facts", [])
    statements = analysis.get("statements", [])
    entities = analysis.get("entities", [])
    background = analysis.get("background", [])
    timeline = analysis.get("timeline", [])
    qa = analysis.get("qa", [])

    facts_count = len(facts)
    statements_count = len(statements)
    entity_count = len(entities)
    background_count = len(background)
    timeline_count = len(timeline)
    qa_count = len(qa)

    importances = [f.get("importance", 0) for f in facts if isinstance(f, dict)]
    avg_fact_importance = sum(importances) / len(importances) if importances else 0.0

    # 정치성 사건은 facts+statements만으로도 정보량이 크지만, 연예/발표성 사건은 facts는 적어도
    # entities(관련 인물의 구체적 역할)·background(과거 맥락)·timeline·qa로 정보가 두터운 경우가
    # 많다 — 이 여섯 축을 모두 합친 information_unit_count로 TOO_SHORT 판정을 한다.
    information_unit_count = (
        facts_count + statements_count + entity_count + background_count + timeline_count + qa_count
    )

    score = (
        facts_count * 2
        + statements_count * 3
        + entity_count * 1.5
        + background_count * 2
        + timeline_count * 1.5
        + qa_count * 1
        + independent_source_count * 1.5
        + min(len(source_items), 10) * 0.5
        + (avg_fact_importance / 100) * 5
    )

    level, min_chars, max_chars = "LOW", 600, 800
    for lvl, threshold, lo, hi in _LEVEL_RANGES:
        if score >= threshold:
            level, min_chars, max_chars = lvl, lo, hi
            break

    return {
        "level": level,
        "min_chars": min_chars,
        "max_chars": max_chars,
        "score": round(score, 1),
        "independent_source_count": independent_source_count,
        "information_unit_count": information_unit_count,
        "facts_count": facts_count,
        "statements_count": statements_count,
        "entity_count": entity_count,
        "background_count": background_count,
        "timeline_count": timeline_count,
        "qa_count": qa_count,
        "avg_fact_importance": round(avg_fact_importance, 1),
        # 구버전 호출부 호환용 별칭 — issue_generator._length_target_block() 등에서 사용.
        "unique_claim_count": facts_count + statements_count,
        "has_background": bool(background),
    }


def compute_extraction_quality(source_items: list[dict], analysis: dict) -> dict:
    """추출 '양'이 아니라 '질'을 가늠하는 보조 지표(0~100) — 배포 전 로그 확인/모니터링 용도.

    compute_richness()가 "이 정보량이면 얼마나 길게 써야 하는가"를 답한다면, 이 함수는 "원문에서
    실제로 충분하고 다양하게 추출됐는가"를 답한다. facts만 잔뜩 뽑히고 entities/background/timeline이
    전부 비어있으면 richness는 어느 정도 나오더라도 extraction quality는 낮게 나올 수 있다 —
    추출이 한쪽으로 치우쳤다는 신호로 활용한다(현재는 로깅 전용, 별도 게이팅에는 쓰지 않는다).
    """
    facts = analysis.get("facts", [])
    entities = analysis.get("entities", [])
    background = analysis.get("background", [])
    timeline = analysis.get("timeline", [])

    importances = [f.get("importance", 0) for f in facts if isinstance(f, dict)]
    avg_importance = sum(importances) / len(importances) if importances else 0.0

    publishers = {
        (item.get("publisher") or "").strip()
        for item in source_items
        if (item.get("publisher") or "").strip()
    }
    independent_source_count = len(publishers) if publishers else len(source_items)
    source_diversity = len({item.get("source") or "naver" for item in source_items}) + (1 if publishers else 0)

    score = 0.0
    score += min(len(facts), 10) * 3
    score += min(len(entities), 6) * 3
    score += 12 if background else 0
    score += 10 if timeline else 0
    score += min(source_diversity, 4) * 3
    score += min(independent_source_count, 6) * 2
    score += (avg_importance / 100) * 16
    score = round(min(100.0, score), 1)

    return {
        "extraction_quality_score": score,
        "avg_fact_importance": round(avg_importance, 1),
        "unique_fact_count": len(facts),
        "unique_entity_count": len(entities),
        "background_count": len(background),
        "timeline_count": len(timeline),
        "source_diversity": source_diversity,
        "independent_source_count": independent_source_count,
    }
