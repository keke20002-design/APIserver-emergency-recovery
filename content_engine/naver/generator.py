"""Naver Generator — 네이버 VIEW 최적화 콘텐츠 생성 (컴포넌트 블록 조립 방식)."""
import json
import logging
import os
import re

from content_engine.base import ContentGenerator, GeneratedContent

logger = logging.getLogger(__name__)

# ── 인텐트별 스타일 가이드 ──────────────────────────────────────────────────

_INTENT_GUIDE: dict[str, str] = {
    "Issue": """형식: 이슈 해설
제목 패턴: "~ 핵심 정리" / "~가 화제인 이유" / "~ 무슨 일?" (20~30자)
구성: 요약박스 → 이슈 배경 → 핵심포인트박스 → 사람들 반응(기사 내 인용) → 핵심 쟁점 정리 → 한줄정리 → CTA
선택 컴포넌트: [D] 인용박스 필수, [C] 비교표 선택""",

    "Reaction": """형식: 반응 정리
제목 패턴: "~ 반응 총정리" / "~ 어떻게 됐나" (20~30자)
구성: 요약박스 → 화제 배경 → 긍정반응(인용박스) → 핵심포인트박스 → 부정반응 → 한줄정리 → CTA
선택 컴포넌트: [D] 인용박스 2개, [B] 핵심포인트 1개""",

    "Summary": """형식: 상황 총정리
제목 패턴: "~ 총정리" / "~ 지금 이 상황" (20~30자)
구성: 요약박스 → 경과 요약 → 비교표(예전vs지금) → 핵심포인트박스 → 앞으로 전망 → 한줄정리 → CTA
선택 컴포넌트: [C] 비교표 필수, [B] 핵심포인트 1개""",

    "Hot": """형식: 화제 분석
제목 패턴: "~ 화제가 된 이유" / "~ 급부상 배경" / "요즘 ~ 관심받는 이유" (20~30자)
구성: 요약박스 → 왜 지금 화제인지 → 핵심포인트박스 → 배경 설명 → 전망 → 한줄정리 → CTA
선택 컴포넌트: [B] 핵심포인트 2개, [C] 비교표 선택""",

    "Prediction": """형식: 전망 분석
제목 패턴: "~ 앞으로 어떻게 될까" / "~ 전망 정리" (20~30자)
구성: 요약박스 → 현재 상황 → 핵심포인트박스 → 예측 시나리오(기사 내 전문가 의견 또는 공식 발표 기반) → 한줄정리 → CTA
선택 컴포넌트: [B] 핵심포인트 1개, [C] 비교표 선택""",

    "Top5": """형식: 순위/핵심 정리
제목 패턴: "~ TOP5" / "~ 핵심 5가지" (15~25자)
구성: 요약박스 → 도입 → 핵심포인트박스 → 체크리스트(5가지) → 한줄정리 → CTA
선택 컴포넌트: [E] 체크리스트 필수, [B] 핵심포인트 1개""",

    "Impact": """형식: 영향 분석
제목 패턴: "~ 실제 영향 정리" / "~ 어떤 변화가 생겼나" (20~30자)
구성: 요약박스 → 발생 사실 → 비교표(영향 전후) → 핵심포인트박스 → 대응 방법(체크리스트) → 한줄정리 → CTA
선택 컴포넌트: [C] 비교표 필수, [E] 체크리스트 선택""",

    "Debate": """형식: 논란 분석
제목 패턴: "~ 논란 쟁점 정리" / "~ 왜 논란인가" (20~30자)
구성: 요약박스 → 논란 경위 → 찬성측(인용박스) → 핵심포인트박스 → 반대측 → 한줄정리 → CTA
선택 컴포넌트: [D] 인용박스 1~2개, [B] 핵심포인트 1개""",

    "Opinion": """형식: 솔직 후기
제목 패턴: "~ 써보니 어떤가" / "~ 장단점 정리" (20~30자)
구성: 요약박스 → 계기 → 좋았던 점(체크리스트) → 핵심포인트박스 → 아쉬운 점 → 한줄정리 → CTA
선택 컴포넌트: [E] 체크리스트 1개, [B] 핵심포인트 1개""",

    "Story": """형식: 상황 정리
제목 패턴: "~ 무슨 일인가" / "~ 지금 상황 정리" (20~30자)
구성: 요약박스 → 사건 발단 → 인용박스 → 핵심포인트박스 → 현재 상황 → 한줄정리 → CTA
선택 컴포넌트: [D] 인용박스 1개, [B] 핵심포인트 1개""",
}

_DEFAULT_INTENT_GUIDE = """형식: 이슈/정보 정리
제목 패턴: "~ 총정리" / "~ 핵심 정리" / "~ 이유" (20~30자)
구성: 요약박스 → 핵심포인트박스 → 본문 → 한줄정리 → CTA
선택 컴포넌트: [B] 핵심포인트 1개"""

# ── HTML 컴포넌트 템플릿 (인라인 스타일, Naver 호환) ──────────────────────

_COMPONENTS_GUIDE = """
==============================
HTML 컴포넌트 템플릿 (아래 그대로 사용할 것)
==============================

[A] 📌 요약박스 — 반드시 본문 맨 앞에 삽입
<div style="background:#f0f7ff;border-left:4px solid #1a73e8;padding:16px 20px;border-radius:8px;margin:16px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="font-weight:700;margin:0 0 8px;font-size:15px;color:#1e40af;">📌 3줄 요약</p><ul style="margin:0;padding-left:20px;line-height:2.0;color:#1f2937;font-size:14px;"><li>이슈/사실 한 줄</li><li>왜 화제인지 한 줄</li><li>앞으로 영향 한 줄</li></ul></div>

[B] 💡 핵심포인트 — 중요한 내용 강조 시 (1~2회)
<div style="background:#fff9e6;border-left:4px solid #f59e0b;padding:14px 18px;border-radius:8px;margin:16px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="font-weight:700;margin:0 0 6px;font-size:14px;color:#92400e;">💡 핵심 포인트</p><p style="margin:0;font-size:15px;line-height:1.8;color:#1f2937;">여기에 핵심 한 줄</p></div>

[C] 📊 비교표 — 예전 vs 지금 / A vs B 비교 시
<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;table-layout:fixed;word-break:keep-all;"><thead><tr style="background:#1a73e8;color:white;"><th style="padding:10px 12px;text-align:left;font-weight:600;">예전</th><th style="padding:10px 12px;text-align:left;font-weight:600;">지금</th></tr></thead><tbody><tr style="background:#f8fafc;"><td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;overflow-wrap:break-word;">항목</td><td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;overflow-wrap:break-word;">항목</td></tr></tbody></table>

[D] 💬 인용박스 — 실제 반응/댓글/의견 소개 시
<div style="background:#f8fafc;border-left:4px solid #6366f1;padding:14px 18px;border-radius:0 8px 8px 0;margin:16px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="margin:0 0 6px;font-weight:700;font-size:13px;color:#6366f1;">💬 이런 반응이 많았습니다</p><p style="margin:0;font-size:14px;line-height:1.9;color:#374151;">"반응 1"<br>"반응 2"</p></div>

[E] ✅ 체크리스트 — 팁/가이드/순위 나열 시
<div style="background:#f0fdf4;border:1px solid #bbf7d0;padding:16px 20px;border-radius:8px;margin:16px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="font-weight:700;margin:0 0 8px;font-size:14px;color:#15803d;">✅ 체크리스트</p><ul style="margin:0;padding-left:20px;line-height:2.0;color:#1f2937;font-size:14px;"><li>항목 1</li><li>항목 2</li></ul></div>

[F] 📝 한줄정리 — 마지막 CTA 직전에 항상 삽입
<div style="background:#1e3a8a;color:white;padding:16px 20px;border-radius:10px;margin:20px 0;text-align:center;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="margin:0 0 4px;font-size:13px;color:#bfdbfe;">📝 한 줄 정리</p><p style="margin:0;font-size:15px;font-weight:700;line-height:1.7;">핵심을 한 줄로 압축</p></div>

[G] ✍️ My Take — 중간에 1회, 독자 시각에서 핵심 관점 한 줄 (공감 카테고리용)
<div style="background:#fffbeb;border-left:3px solid #f59e0b;padding:14px 18px;border-radius:0 8px 8px 0;margin:16px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#92400e;">✍️ My Take</p><p style="margin:0;font-size:14px;line-height:1.8;color:#1f2937;">독자 관점에서 이 주제의 핵심을 한 문장으로 (기사 내용 기반, 꾸며낸 감정 금지)</p></div>

[H] 📍 구분선 잠깐박스 — 섹션 전환 시 시각적 호흡 제공 (1~2회)
<div style="margin:20px 0;text-align:center;color:#6b7280;font-size:13px;">━━━━━━━━━━━━━━━━━━━━<br>📍 여기서 잠깐<br>━━━━━━━━━━━━━━━━━━━━</div>

[I] 📋 인라인 카드 — 3~4가지 요점을 체크리스트 없이 읽기 좋게 나열
<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:14px 18px;margin:16px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="font-weight:700;margin:0 0 8px;font-size:14px;color:#0369a1;">📋 카드 제목 (예: SNS 때문에 달라진 점)</p><p style="margin:0;font-size:14px;line-height:2.0;color:#1f2937;">✔ 항목 1<br>✔ 항목 2<br>✔ 항목 3</p></div>

[J] 📊 핵심 수치 그리드 — 주요 통계/숫자가 있을 때 시각적으로 강조 (선택)
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0;box-sizing:border-box;"><div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:14px;text-align:center;box-sizing:border-box;"><p style="font-size:22px;font-weight:800;color:#1a73e8;margin:0;">숫자</p><p style="font-size:13px;color:#6b7280;margin:4px 0 0;">설명 레이블</p></div><div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:14px;text-align:center;box-sizing:border-box;"><p style="font-size:22px;font-weight:800;color:#1a73e8;margin:0;">숫자</p><p style="font-size:13px;color:#6b7280;margin:4px 0 0;">설명 레이블</p></div><div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:14px;text-align:center;box-sizing:border-box;"><p style="font-size:22px;font-weight:800;color:#1a73e8;margin:0;">숫자</p><p style="font-size:13px;color:#6b7280;margin:4px 0 0;">설명 레이블</p></div></div>

[K] 🙋 FAQ — 글 맨 끝에 반드시 추가 (독자가 실제로 궁금해할 질문 3개)
<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:18px 20px;margin:20px 0;box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"><p style="font-weight:700;margin:0 0 14px;font-size:15px;color:#1f2937;">🙋 자주 묻는 질문</p><div style="margin-bottom:12px;"><p style="font-weight:700;font-size:14px;color:#1a73e8;margin:0 0 4px;">Q. 질문 1?</p><p style="font-size:14px;color:#374151;margin:0;line-height:1.8;">A. 답변 1</p></div><div style="margin-bottom:12px;"><p style="font-weight:700;font-size:14px;color:#1a73e8;margin:0 0 4px;">Q. 질문 2?</p><p style="font-size:14px;color:#374151;margin:0;line-height:1.8;">A. 답변 2</p></div><div><p style="font-weight:700;font-size:14px;color:#1a73e8;margin:0 0 4px;">Q. 질문 3?</p><p style="font-size:14px;color:#374151;margin:0;line-height:1.8;">A. 답변 3</p></div></div>
"""

# 카테고리 분류: 팩트 중심 vs 공감 허용
_FACT_CATEGORIES = {"스포츠", "IT", "AI", "주식", "이슈", "자동차", "게임"}
_EMPATHY_CATEGORIES = {"연예", "생활", "쇼핑"}

# ── 공통 JSON 출력 형식 ──────────────────────────────────────────────────────
_JSON_FORMAT = """
==============================
출력 형식 (JSON만, 코드블록 없이)
content는 HTML 마크업만 포함. 별도 줄바꿈 문자 불필요.
content 안에 큰따옴표는 반드시 \\" 로 이스케이프
==============================
{{
  "title": "클릭률 높은 제목 (20~35자, 위 인텐트 패턴 중 하나 필수)",
  "meta_description": "네이버 검색 스니펫 (80~120자)",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "summary": ["핵심 사실 한 줄", "왜 화제인지 한 줄", "앞으로 영향 한 줄"],
  "traffic_score": {{
    "score": 75,
    "competition": 40,
    "trend_level": "WARM",
    "reason": ["점수 근거 한 줄", "점수 근거 한 줄"]
  }},
  "content": "[A]요약박스<p>도입문단</p><h2>소제목</h2><p>본문</p>[B]핵심포인트[F]한줄정리<p>CTA문단</p>"
}}

traffic_score 채점 기준:
- 현재 뉴스 가치/시의성 (최근 화제 여부)
- 일반 대중 관심도 (연령층 무관 흥미)
- 검색될 가능성 (키워드 검색량 추정)
- SNS 확산 가능성
- 클릭 유도 가능성 (제목 매력)
- 카테고리 인기도
- 키워드 트렌드 강도
trend_level: 80+ HOT, 60~79 WARM, 60 미만 NORMAL
""".strip()

# ── 팩트 중심 프롬프트 (이슈·스포츠·IT·주식·자동차·게임·AI) ─────────────────
_NAVER_PROMPT_FACT = """
당신은 네이버 블로그에 글을 쓰는 블로거다.
뉴스 기사를 바탕으로 독자가 궁금해할 내용을 블로그 형식으로 풀어낸다.
사실에 근거하되, 딱딱한 신문 투가 아닌 자연스럽고 읽기 편한 블로그 문체로 작성한다.

키워드: {keyword}
카테고리: {category}

★★ 핵심 제약 (절대 위반 금지) ★★
이 글의 주제는 오직 "{keyword}"이다.
- 제목 첫 단어 또는 핵심어에 "{keyword}"의 주요 단어가 반드시 포함되어야 한다.
- 뉴스 기사에 다른 주제가 섞여 있어도 "{keyword}" 외 주제로 글을 쓰면 완전히 틀린 답이다.
- 관련 기사가 없으면 "최근 {keyword} 관련 논의"를 중심으로 작성하되 제목은 반드시 {keyword}를 포함한다.

★★ 구체성 필수 (절대 위반 금지) ★★
아래 뉴스 기사에 나온 고유명사·수치를 본문에 반드시 인용해서 쓴다 — 인물/캐릭터/작품/브랜드/플랫폼 이름,
금액·단가·비율·기간 등 숫자, 국가·지역, 출처 매체명 중 기사에 등장하는 것을 최소 3개 이상 본문에 그대로 명시한다.
"AI가 시장을 바꾸고 있다", "다양한 상황에 유연하게 대응한다", "24시간 활동 가능하다"처럼 어느 기사에나
붙여도 말이 되는 뻔한 일반론 문장으로 문단을 채우지 않는다 — 그런 문장을 쓰려면 반드시 기사에 나온
구체적 근거(누가, 어디서, 얼마에, 며칠 만에 등)를 같은 문장 또는 바로 다음 문장에 붙인다.
기사에 숫자(금액/순위/기간/횟수 등)가 하나라도 있으면 [J] 핵심 수치 그리드에 그 실제 숫자를 넣어 활용한다.
제목만 보고 상상해서 쓴 것처럼 읽히는 추상적인 글은 완전히 틀린 답이다.

=== 참고 뉴스 기사 (날짜 최신순) ===
{news_context}

★ 날짜 주의: 오늘은 {today}다. 기사 날짜를 반드시 확인하고, 가장 최근(최신) 기사의 내용을 중심으로 작성한다.
★ 오래된 사건(1개월 이상 지난 기사)을 메인 주제로 쓰지 않는다. 최신 흐름이 없으면 "현재 어떤 논의가 이어지고 있는지"를 중심으로 작성한다.
★★ "날짜 미상"으로 표시된 기사는 절대 오늘/최근 뉴스로 취급하지 않는다 — 실제 게재 시점을 알 수 없다는 뜻이다.
근거 기사가 전부 "날짜 미상"이거나 30일 이상 지난 것뿐이라면, 제목·본문에 "지금", "오늘", "최근", "지금 상황" 같은
현재진행형 표현을 쓰지 않는다. 이 경우 "~는 어떤 사건인가" 식으로 사실 자체를 설명하는 데 그치고,
"지금 이 시점에 벌어지고 있는 일"처럼 서술하지 않는다.
★ 과거 연도 사건 금지: "2025년", "2024년" 등 이전 연도에 종결된 사건(예: 2025학년도 입시, 2024년 사건)을 현재 이슈처럼 제목에 쓰지 않는다. 제목에 연도를 쓸 때는 반드시 2026년 기준이어야 한다.
★★ 단일 사건 원칙 (매우 중요): {keyword}가 유명 인물/기업이면 검색된 기사들이 서로 무관한
여러 사건(예: 이적/계약 소식, 논란, 정치권 언급, 광고 모델 계약 등)을 동시에 다룰 수 있다.
이 경우 그중 "가장 최신이고 가장 비중 있게 보도된 사건" 딱 하나만 골라 그 사건에 대해서만 쓴다.
서로 다른 사건들을 하나로 억지로 엮거나 짜깁기하지 않는다 — 무관한 사건은 언급 자체를 생략한다.
예: 이적 뉴스와 감독 논란 기사가 섞여 있다면, 이적이 더 최신/비중 있는 사건이면 이적 얘기만 쓰고
감독 논란은 아예 언급하지 않는다 (둘을 한 문단에 섞어서 "이런저런 이유로 화제"라고 뭉뚱그리지 않는다).

=== 이번 글 형식 ===
{intent_guide}

{components_guide}

==============================
content 구성 순서 (반드시 이 순서로)
==============================
1. [A] 요약박스 (summary 필드의 3가지 항목을 li로 채울 것)
2. 도입 — 3~4문장, 독자 시각에서 자연스럽게 흥미를 유도
3. <h2>질문형/대화형 소제목</h2> + 본문 4~5문장 (문단 2개로 나눠 작성)
4. [B] 핵심포인트박스 또는 [J] 핵심 수치 그리드 (수치가 있을 때)
5. <h2>질문형 소제목</h2> + 본문 4~5문장 (문단 2개로 나눠 작성)
6. 인텐트에 맞는 선택 컴포넌트 ([C] [D] [E] [I] 중 하나)
7. <h2>질문형 소제목</h2> + 본문 4~5문장 (문단 2개로 나눠 작성)
8. [H] 구분선 잠깐박스
9. <h2>질문형 소제목</h2> + 본문 3~4문장
10. [F] 한줄정리박스
11. [K] FAQ — 독자가 실제로 검색할 질문 3개 (필수, 항상 마지막에)

★ 분량: 본문 전체 공백 포함 2200자 이상을 목표로 한다. 요약만 하지 말고 각 섹션마다
배경·구체적 근거·상황 설명을 충분히 덧붙여 내용을 채운다 (단, 기사에 없는 내용을 지어내지는 않는다).

==============================
기본 구성 흐름
==============================
핵심 요약 → 왜 화제인가? → 어떤 내용인가? → 핵심 쟁점 / 포인트 → 앞으로 어떻게 되나? → 한줄정리 → FAQ

==============================
문체 규칙 (팩트 기반 블로그 스타일)
==============================
- 수집된 뉴스 기사에 있는 사실(Fact)만 내용으로 사용한다
- 기사에 없는 내용을 꾸며내거나 단정 짓지 않는다
- 독자 시각으로 쓴다 — "저는" 대신 "많은 분들이", "눈에 띄는 건", "이 부분에서 흥미로운 점은"
- 블로그 자연어 허용: "이게 왜 화제가 됐냐면", "눈여겨볼 부분이 있어요", "이 부분이 포인트예요"
- 질문은 내용과 연결된 것만: "왜 지금 이 얘기가 나오는 걸까요?", "가장 많이 궁금해하는 부분은 뭘까요?"
- "여러분은 어떻게 생각하세요?" 반복 금지 (내용과 연결된 질문만)
- 소제목은 질문형 또는 대화형 ("왜 지금 이 얘기가 나올까?", "실제로 어떤 변화가 생겼을까?")
- 문단은 최대 2~3문장. 자주 줄바꿈.
- 정치·사회·경제 글에서 특정 인물을 미화하거나 비하하는 표현 금지

==============================
제목 규칙
==============================
- 제목은 기사 내용을 정확하게 반영한 서술형 또는 질문형으로 작성
- 금지 제목 표현: "설마", "충격", "알고 보니", "이것 때문", "난리인 이유", "뜨는 이유", "갑자기", "대박", "반전"
- 금지 제목 패턴: "~ 때문?" / "설마 ~?" / "알고 보니 ~" / "충격 ~" 형태
- 허용 제목 패턴: "~ 핵심 정리" / "~ 화제가 된 이유" / "~ 무슨 일인가" / "~ 전망 정리" / "~ 논란 쟁점"

==============================
하이라이트 규칙
==============================
- 독자가 반드시 기억해야 할 핵심 어구(2~5단어)는 <mark>어구</mark> 태그로 강조
- 문단당 최대 2개, 글 전체 최대 6개
- 예: "이 결정이 <mark>시장 전체에 미치는 영향</mark>은 생각보다 큽니다"

==============================
절대 금지 문장/표현
==============================
- "저는", "저도", "제가", "개인적으로" (1인칭 주어)
- "생각보다", "놀랐다", "의외였습니다", "드라마 같았습니다", "충격이었다"
- "저도 찾아봤는데요", "찾아보니", "처음엔", "솔직히"
- "~인 것 같아요", "~인 듯 합니다" (근거 없는 추측)
- AI가 꾸며낸 감정·상상·추측으로 새로운 사실 생성
- 기사에 없는 내용을 사실처럼 서술
- 오늘은 알아보겠습니다 / 살펴보겠습니다 / 소개해드리겠습니다
- 이번 글에서는 / 안녕하세요 / 도움이 되셨길 바랍니다
- ~입니다. ~입니다. ~입니다. (같은 어미 3회 연속)
- "여러분은 어떻게 생각하세요?" 반복 (내용 무관한 질문 금지)
- [G] 혼잣말 코멘트박스 사용 금지

==============================
도입부 패턴 (아래 중 하나로 시작)
==============================
패턴 A:
"요즘 {keyword} 얘기가 꽤 많이 보이죠.\n왜 지금 이 얘기가 나오는 건지, 핵심만 정리해봤어요."

패턴 B:
"{keyword}, 지금 왜 이렇게 화제가 된 걸까요?\n기사들을 보면 한두 가지가 아니라 여러 이슈가 겹쳐 있더라고요."

패턴 C:
"최근 {keyword} 관련 소식이 잇따르고 있어요.\n어떤 사건부터 보면 이해가 쉬울지, 흐름을 정리해봤습니다."

{json_format}
""".strip()

# ── 공감 허용 프롬프트 (연예·생활·쇼핑) ─────────────────────────────────────
_NAVER_PROMPT_EMPATHY = """
당신은 네이버 블로그에 글을 쓰는 블로거다.
아래 뉴스 기사를 바탕으로 사실을 중심으로 쓰되, 독자와 자연스럽게 공감하는 문체를 사용한다.

키워드: {keyword}
카테고리: {category}

★★ 핵심 제약 (절대 위반 금지) ★★
이 글의 주제는 오직 "{keyword}"이다.
- 제목 첫 단어 또는 핵심어에 "{keyword}"의 주요 단어가 반드시 포함되어야 한다.
- 뉴스 기사에 다른 주제가 섞여 있어도 "{keyword}" 외 주제로 글을 쓰면 완전히 틀린 답이다.
- 관련 기사가 없으면 "최근 {keyword} 관련 논의"를 중심으로 작성하되 제목은 반드시 {keyword}를 포함한다.

★★ 구체성 필수 (절대 위반 금지) ★★
아래 뉴스 기사에 나온 고유명사·수치를 본문에 반드시 인용해서 쓴다 — 인물/캐릭터/작품/브랜드/플랫폼 이름,
금액·단가·비율·기간 등 숫자, 국가·지역, 출처 매체명 중 기사에 등장하는 것을 최소 3개 이상 본문에 그대로 명시한다.
"AI가 시장을 바꾸고 있다", "다양한 상황에 유연하게 대응한다", "24시간 활동 가능하다"처럼 어느 기사에나
붙여도 말이 되는 뻔한 일반론 문장으로 문단을 채우지 않는다 — 그런 문장을 쓰려면 반드시 기사에 나온
구체적 근거(누가, 어디서, 얼마에, 며칠 만에 등)를 같은 문장 또는 바로 다음 문장에 붙인다.
기사에 숫자(금액/순위/기간/횟수 등)가 하나라도 있으면 [J] 핵심 수치 그리드에 그 실제 숫자를 넣어 활용한다.
제목만 보고 상상해서 쓴 것처럼 읽히는 추상적인 글은 완전히 틀린 답이다.

=== 참고 뉴스 기사 (날짜 최신순) ===
{news_context}

★ 날짜 주의: 오늘은 {today}다. 가장 최근 기사 내용을 중심으로 작성. 오래된 사건(1개월 이상)은 메인 주제로 쓰지 않는다.
★★ "날짜 미상"으로 표시된 기사는 절대 오늘/최근 뉴스로 취급하지 않는다 — 실제 게재 시점을 알 수 없다는 뜻이다.
근거 기사가 전부 "날짜 미상"이거나 30일 이상 지난 것뿐이라면, 제목·본문에 "지금", "오늘", "최근", "지금 상황" 같은
현재진행형 표현을 쓰지 않는다.
★ 과거 연도 사건 금지: "2025년", "2024년" 등 이전 연도에 종결된 사건을 현재 이슈처럼 제목에 쓰지 않는다. 제목에 연도를 쓸 때는 반드시 2026년 기준이어야 한다.
★★ 단일 사건 원칙 (매우 중요): {keyword}가 유명 인물이면 검색된 기사들이 서로 무관한 여러 사건
(예: 열애설, 이적/계약 소식, 논란, 광고 계약 등)을 동시에 다룰 수 있다. 이 경우 그중 "가장 최신이고
가장 비중 있게 보도된 사건" 딱 하나만 골라 그것만 쓴다. 서로 다른 사건을 하나로 억지로 엮지 않는다 —
무관한 사건은 언급 자체를 생략한다.

=== 이번 글 형식 ===
{intent_guide}

{components_guide}

==============================
content 구성 순서 (반드시 이 순서로)
==============================
1. [A] 요약박스 (summary 필드의 3가지 항목을 li로 채울 것)
2. 도입 — 짧게 끊어 3~4줄, 공감 또는 질문으로 끝낼 것
3. <h2>질문형 소제목</h2> + 본문 3~4문장
4. [G] 혼잣말 코멘트박스 (1회만 — 뉴스 기사 내용에 대한 반응, 꾸며낸 감정 금지)
5. <h2>질문형 소제목</h2> + 본문 3~4문장
6. [B] 핵심포인트박스
7. <h2>질문형 소제목</h2> + 본문 3~4문장
8. [H] 구분선 잠깐박스 (섹션 전환)
9. <h2>질문형 소제목</h2> + 본문 3~4문장
10. 인텐트에 맞는 선택 컴포넌트 ([C] [D] [E] [I] 중 해당하는 것)
11. [F] 한줄정리박스
12. [K] FAQ — 독자가 실제로 궁금해할 질문 3개 (필수, 항상 마지막에)

★ 분량: 본문 전체 공백 포함 2000자 이상을 목표로 한다. 요약만 하지 말고 각 섹션마다
배경·구체적인 내용·독자가 궁금해할 디테일을 충분히 덧붙인다 (단, 기사에 없는 내용을 지어내지는 않는다).

==============================
문체 규칙 (공감 블로그 스타일)
==============================
- 뉴스 기사에 있는 사실을 기반으로 작성한다
- 기사에 없는 내용을 꾸며내거나 추측하지 않는다
- 공감 표현은 기사 내용에 대한 반응으로만 허용 ("이 부분이 화제가 된 이유가 있네요", "많은 분들이 놀란 이유를 알 것 같아요")
- "저는 놀랐습니다", "드라마 같았습니다", "의외였습니다" 같은 근거 없는 감정 표현 금지
- 소제목은 질문형 또는 대화형
- 문단은 최대 2~3문장. 자주 줄바꿈.
- 글 전체에서 독자에게 직접 질문하는 문장 최소 2개 포함

==============================
절대 금지
==============================
- 기사에 없는 내용을 사실처럼 서술
- "저는 솔직히 좀 놀랐거든요", "드라마 같았습니다", "연예인급 화제였습니다"
- "~인 것 같아요" (뇌피셜 추측 표현)
- AI가 꾸며낸 감정·상상으로 새로운 사실 생성
- 오늘은 알아보겠습니다 / 살펴보겠습니다 / 소개해드리겠습니다
- 이번 글에서는 / 안녕하세요 / 도움이 되셨길 바랍니다
- ~입니다. ~입니다. ~입니다. (같은 어미 3회 연속)

==============================
하이라이트 규칙
==============================
- 독자가 반드시 기억해야 할 핵심 어구(2~5단어)는 <mark>어구</mark> 태그로 강조
- 문단당 최대 2개, 글 전체 최대 6개

==============================
도입부 패턴 (아래 중 하나로 시작)
==============================
패턴 A:
"요즘 {keyword} 얘기가 많이 보이던데, 여러분도 보셨나요?\n기사들을 정리해봤습니다."

패턴 B:
"{keyword}, 많은 분들이 관심 갖고 계신 것 같아요.\n어떤 내용인지 같이 살펴볼게요."

패턴 C:
"최근 {keyword} 관련 소식이 잇따르고 있어요.\n핵심 내용을 정리했습니다."

{json_format}
""".strip()

# ── 후처리 패턴 ────────────────────────────────────────────────────────────

_BAD_OPENER = re.compile(
    r"(안녕하세요[^.。]*[.。]|이번 (글|포스팅)에서는[^.。]*[.。]|오늘은[^.。]*(알아보|살펴보|소개)[^.。]*[.。])",
    re.IGNORECASE,
)
_REPEATED_ENDINGS = re.compile(r"(입니다\.\s*){3,}")

_H2_PLAIN = re.compile(r"<h2>")
_H2_STYLED = '<h2 style="border-left:4px solid #1a73e8;padding-left:14px;color:#1e3a5f;font-size:18px;font-weight:700;margin:24px 0 10px;">'

# 컴포넌트 교정: Gemini가 잘못된 색상/아이콘을 써도 텍스트 식별자로 강제 교정
_BOX_BASE = "box-sizing:border-box;word-break:keep-all;overflow-wrap:break-word;"
_COMP_FIX = [
    (
        # [A] 요약박스 → 파란색
        re.compile(r'<div[^>]*>(\s*(?:<[^>]+>)*\s*)[^\n<]*3줄\s*요약', re.DOTALL),
        f'<div style="background:#f0f7ff;border-left:4px solid #1a73e8;padding:16px 20px;border-radius:8px;margin:16px 0;{_BOX_BASE}">\\1📌 3줄 요약',
    ),
    (
        # [B] 핵심포인트 → 노란색
        re.compile(r'<div[^>]*>(\s*(?:<[^>]+>)*\s*)[^\n<]*핵심\s*포인트', re.DOTALL),
        f'<div style="background:#fff9e6;border-left:4px solid #f59e0b;padding:14px 18px;border-radius:8px;margin:16px 0;{_BOX_BASE}">\\1💡 핵심 포인트',
    ),
    (
        # [F] 한줄정리 → 다크 네이비
        re.compile(r'<div[^>]*>(\s*(?:<[^>]+>)*\s*)[^\n<]*한\s*줄\s*정리', re.DOTALL),
        f'<div style="background:#1e3a8a;color:white;padding:16px 20px;border-radius:10px;margin:20px 0;text-align:center;{_BOX_BASE}">\\1📝 한 줄 정리',
    ),
]


def _style_headings(content: str) -> str:
    """plain <h2> 태그에 WordPress 스타일 left-border 인라인 스타일 적용."""
    return _H2_PLAIN.sub(_H2_STYLED, content)


def _fix_component_styles(content: str) -> str:
    """컴포넌트 배경색이 잘못 생성된 경우 텍스트 식별자 기준으로 강제 교정."""
    for pattern, replacement in _COMP_FIX:
        content = pattern.sub(replacement, content, count=1)
    return content


_BOX_A_OPEN = (
    'background:#f0f7ff;border-left:4px solid #1a73e8;'
    'padding:16px 20px;border-radius:8px;margin:16px 0;'
    + _BOX_BASE
)
_BOX_B_OPEN = (
    'background:#fff9e6;border-left:4px solid #f59e0b;'
    'padding:14px 18px;border-radius:8px;margin:16px 0;'
    + _BOX_BASE
)

# Gemini가 [A]/[B] 라벨을 텍스트로 쓴 경우 — <p> 래핑 포함 여부 둘 다 매칭
_LABEL_A = re.compile(r'(?:<p[^>]*>)?\[A\][^<\n]*(?:</p>)?[ \t]*\n?\s*', re.DOTALL)
_LABEL_B = re.compile(r'(?:<p[^>]*>)?\[B\][^<\n]*(?:</p>)?[ \t]*\n?\s*', re.DOTALL)
_LABEL_ANY = re.compile(r'(?:<p[^>]*>)?\[[A-K]\][^<\n]*(?:</p>)?[ \t]*\n?')


def _replace_component_labels(content: str) -> str:
    """Gemini가 [A]요약박스 등 라벨을 텍스트로 출력한 경우 HTML 박스로 교체하거나 제거."""
    new_content = content
    ma = _LABEL_A.search(new_content)
    if ma:
        after_label = new_content[ma.end():]
        ul_m = re.match(r'\s*(<ul[^>]*>[\s\S]*?</ul>)', after_label)
        if ul_m:
            replacement = (
                f'<div style="{_BOX_A_OPEN}">'
                f'<p style="font-weight:700;margin:0 0 8px;font-size:15px;color:#1e40af;">📌 3줄 요약</p>'
                f'{ul_m.group(1)}</div>'
            )
            new_content = new_content[:ma.start()] + replacement + after_label[ul_m.end():]
        else:
            new_content = new_content[:ma.start()] + after_label

    # [B] 라벨 + 바로 뒤 <ul> 또는 <p> → 노란 핵심포인트박스로 래핑
    mb = _LABEL_B.search(new_content)
    if mb:
        after_label = new_content[mb.end():]
        block_m = re.match(r'\s*(<ul[^>]*>[\s\S]*?</ul>|<p[^>]*>[\s\S]*?</p>)', after_label)
        if block_m:
            replacement = (
                f'<div style="{_BOX_B_OPEN}">'
                f'<p style="font-weight:700;margin:0 0 6px;font-size:14px;color:#92400e;">💡 핵심 포인트</p>'
                f'{block_m.group(1)}</div>'
            )
            new_content = new_content[:mb.start()] + replacement + after_label[block_m.end():]
        else:
            new_content = new_content[:mb.start()] + after_label

    # 나머지 [C]~[K] 라벨 및 놓친 [A][B] 라벨 — 모두 제거
    new_content = _LABEL_ANY.sub('', new_content)

    return new_content


# ── JSON 파서 (이스케이프 오류 허용) ──────────────────────────────────────

def _scan_json_string(text: str, start: int) -> tuple[str, int]:
    """
    JSON string 파싱: start는 여는 '"' 다음 위치.
    이스케이프 안 된 '"' 가 나와도, 뒤에 , } ] 가 오면 종료, 아니면 리터럴로 처리.
    """
    chars: list[str] = []
    pos = start
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == "\\" and pos + 1 < n:
            esc = text[pos + 1]
            chars.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(esc, esc))
            pos += 2
        elif ch == '"':
            tail = text[pos + 1:pos + 10].lstrip(" \t\r\n")
            if not tail or tail[0] in (",", "}", "]"):
                return "".join(chars), pos + 1
            chars.append('"')
            pos += 1
        else:
            chars.append(ch)
            pos += 1
    return "".join(chars), pos


def _extract_field(text: str, key: str) -> str | None:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"', re.DOTALL)
    m = pattern.search(text)
    if not m:
        return None
    value, _ = _scan_json_string(text, m.end())
    return value


def _extract_traffic_score(text: str) -> dict | None:
    """traffic_score 중첩 객체를 raw 텍스트에서 직접 추출."""
    m = re.search(r'"traffic_score"\s*:\s*\{([^}]+)\}', text, re.DOTALL)
    if not m:
        return None
    inner = m.group(1)
    try:
        return json.loads("{" + inner + "}")
    except Exception:
        score_m = re.search(r'"score"\s*:\s*(\d+)', inner)
        comp_m  = re.search(r'"competition"\s*:\s*(\d+)', inner)
        trend_m = re.search(r'"trend_level"\s*:\s*"([^"]+)"', inner)
        reason_m = re.findall(r'"([^"]{5,100})"', inner)
        if score_m:
            return {
                "score": int(score_m.group(1)),
                "competition": int(comp_m.group(1)) if comp_m else None,
                "trend_level": trend_m.group(1) if trend_m else None,
                "reason": reason_m[:2],
            }
    return None


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("JSON parse failed — fallback to char-scan extraction")
        title   = _extract_field(text, "title")
        meta    = _extract_field(text, "meta_description") or ""
        content = _extract_field(text, "content") or ""
        tags_m    = re.search(r'"tags"\s*:\s*(\[.*?\])', text, re.DOTALL)
        summary_m = re.search(r'"summary"\s*:\s*(\[.*?\])', text, re.DOTALL)
        tags    = json.loads(tags_m.group(1))    if tags_m    else []
        summary = json.loads(summary_m.group(1)) if summary_m else []
        ts = _extract_traffic_score(text)
        if title and content:
            return {
                "title": title, "meta_description": meta,
                "tags": tags, "summary": summary, "content": content,
                "traffic_score": ts,
            }
        html_m  = re.search(r'(<h[123]|<p|<div|<table)', text)
        title_m = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if html_m and title_m:
            return {
                "title": title_m.group(1), "meta_description": meta,
                "tags": [], "summary": [], "content": text[html_m.start():],
                "traffic_score": ts,
            }
        raise


_INTENT_SUFFIXES = [
    "가 이슈인 이유", "이 이슈인 이유", " 이슈인 이유",
    " 반응 총정리", " 상황 정리", " 화제가 된 이유",
    " 앞으로 어떻게 될까", " 핵심 5가지", " 실제 영향은",
    " 논란 총정리", " 솔직한 생각", " 지금 상황",
]


def _extract_entity(keyword: str) -> str:
    """키워드에서 인텐트 접미사를 제거해 엔티티 이름만 추출. 뉴스 검색용."""
    kw = keyword.strip()
    if kw.startswith("요즘 "):
        kw = kw[3:]
    for suffix in _INTENT_SUFFIXES:
        if kw.endswith(suffix):
            return kw[: -len(suffix)].strip()
    return kw


# ── Generator ──────────────────────────────────────────────────────────────

def _build_news_context(articles: list[dict], max_articles: int = 8) -> str:
    """뉴스 기사 목록을 프롬프트용 텍스트로 변환 (30일 초과 기사 제외)."""
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone, timedelta

    if not articles:
        return "(수집된 기사 없음 — 키워드 기반으로만 작성)"

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=30)
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    recent, old = [], []
    for a in articles:
        pub_raw = a.get("pub_date", "") or a.get("pubDate", "")
        pub_dt = None
        if pub_raw:
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pub_dt = None
        if pub_dt is not None and pub_dt >= cutoff:
            recent.append((a, pub_dt))
        else:
            # 날짜 미상(예: anchor_title처럼 pubDate 없이 강제 주입된 기사) 또는
            # 파싱 실패는 "최신"으로 단정하지 않고 old로 분류한다 —
            # 실제 날짜를 모르는 기사를 오늘 기사처럼 취급하면 오래된 사건을
            # "지금 상황"으로 잘못 서술하게 된다.
            old.append((a, pub_dt))

    # old는 날짜가 있는 것부터 최신순, 날짜 미상은 맨 뒤로
    old.sort(key=lambda x: x[1] or _epoch, reverse=True)

    # 30일 이내 기사가 3개 미만이면 old에서 보충 (최신순, 날짜 미상은 맨 뒤)
    pool = recent if len(recent) >= 3 else recent + old

    lines = []
    for i, (a, pub_dt) in enumerate(pool[:max_articles], 1):
        title   = re.sub(r"<[^>]+>", "", a.get("title", "")).strip()
        desc    = re.sub(r"<[^>]+>", "", a.get("description", "")).strip()
        pub_str = pub_dt.strftime("%Y-%m-%d") if pub_dt else "날짜 미상"
        lines.append(f"[기사 {i}] ({pub_str}) {title}")
        if desc:
            lines.append(f"  → {desc[:220]}")
    return "\n".join(lines)


class NaverKRGenerator(ContentGenerator):
    """네이버 블로그용 한국어 콘텐츠 생성 (카테고리별 문체 분리 + 뉴스 기사 주입)."""
    platform = "naver"
    language = "ko"

    async def generate(self, keyword: str, topic: str, intent: str = "", anchor_title: str = "") -> GeneratedContent:
        from google import genai
        from google.genai import types as genai_types
        from services.naver_api import search_news

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")
        client = genai.Client(api_key=api_key)

        # 뉴스 기사 수집 — 전체 키워드 우선, 엔티티로 보충
        entity = _extract_entity(keyword)
        kw_articles: list[dict] = []
        entity_articles: list[dict] = []

        # 1차: 전체 키워드 검색 (구체적인 키워드일수록 더 정확한 기사)
        if entity != keyword:
            try:
                kw_articles = await search_news(keyword, display=10, sort="date")
            except Exception as e:
                logger.warning("News fetch (full kw) failed for '%s': %s", keyword, e)

        # 2차: 엔티티 검색으로 보충
        try:
            entity_articles = await search_news(entity, display=20, sort="date")
        except Exception as e:
            logger.warning("News fetch failed for '%s': %s", entity, e)

        def _clean(text: str) -> str:
            return re.sub(r"<[^>]+>", "", text).lower()

        entity_lower = entity.lower()
        # 멀티워드 엔티티(예: "BTS 프로젝트 망했다")는 제목에 정확히 그 문구가
        # 통째로 들어있는 경우가 거의 없다. 단어 대부분(최소 절반, 1개 이하 누락 허용)이
        # 제목에 있으면 관련 기사로 인정 — 완전 무관한 기사가 섞여 들어오는 것만 막는다.
        _entity_words = [w for w in entity.split() if len(w) >= 2]

        def _matches_entity(title: str) -> bool:
            t = _clean(title)
            if not _entity_words:
                return entity_lower in t
            if len(_entity_words) == 1:
                return _entity_words[0].lower() in t
            hits = sum(1 for w in _entity_words if w.lower() in t)
            return hits >= max(1, len(_entity_words) - 1)

        # 전체 키워드로 검색된 기사가 충분하면 그것만 사용 (더 주제 집중)
        if len(kw_articles) >= 3:
            articles = kw_articles
        else:
            # 엔티티 기사 중 제목이 실제로 엔티티와 관련된 것만 보충
            seen = set(a.get("link", "") for a in kw_articles)
            supplement = [
                a for a in entity_articles
                if a.get("link", "") not in seen
                and _matches_entity(a.get("title", ""))
            ]
            raw_articles = kw_articles + supplement
            title_filtered = [
                a for a in raw_articles
                if _matches_entity(a.get("title", ""))
            ]
            # 무관한 기사를 "참고 뉴스"로 위장해 억지 서술을 만들지 않도록,
            # 관련성 검증 없는 entity_articles[:5] 블라인드 폴백은 쓰지 않는다.
            articles = title_filtered if title_filtered else raw_articles

        # TrendLab 등 호출자가 이 토픽을 뽑아낸 원 출처 기사 제목을 넘겨준 경우,
        # 재검색이 그 기사를 다시 못 찾아오더라도 항상 근거로 포함시킨다.
        # (검색 재현이 안 돼서 실제로 존재하는 사실을 "확인 안 됨"으로 잘못 단정하는 것을 방지)
        if anchor_title and not any(
            _clean(anchor_title) == _clean(a.get("title", "")) for a in articles
        ):
            articles = [{"title": anchor_title, "description": "", "pubDate": ""}] + articles

        if not articles:
            raise RuntimeError(
                f"'{keyword}' 관련 실제 뉴스 근거를 찾지 못함 — "
                f"무관한 기사로 짜맞춘 글이 생성되는 것을 막기 위해 생성을 중단합니다."
            )

        # ── 동명이인 혼재 감지 및 필터 ──────────────────────────────────────────
        # 사람 이름 패턴(2~4글자 한글)이고 카테고리가 연예/스포츠이면
        # 기사 설명의 주요 컨텍스트 키워드를 클러스터링해 단일 컨텍스트만 남김
        _is_person_name = bool(re.match(r"^[가-힣]{2,4}$", entity))
        if _is_person_name and topic in ("연예", "스포츠", "이슈") and len(articles) >= 4:
            # 카테고리 힌트 키워드
            _CAT_HINTS = {
                "연예": ["드라마", "영화", "배우", "가수", "방송", "앨범", "촬영", "열애", "결혼", "팬", "시청률"],
                "스포츠": ["경기", "골", "득점", "선수", "팀", "우승", "감독", "리그", "시즌"],
                "이슈": ["논란", "사건", "경찰", "수사", "법원", "재판"],
            }
            hints = _CAT_HINTS.get(topic, [])
            if hints:
                def _ctx_score(a: dict) -> int:
                    text = _clean(a.get("title", "") + " " + a.get("description", ""))
                    return sum(1 for h in hints if h in text)

                scored_arts = [(a, _ctx_score(a)) for a in articles]
                hint_matched = [a for a, s in scored_arts if s > 0]
                # 힌트 기사가 전체의 절반 이상이면 힌트 기사만 사용
                if len(hint_matched) >= max(2, len(articles) // 2):
                    articles = hint_matched
                    logger.info("Homonym filter: kept %d/%d articles matching category hints",
                                len(articles), len(scored_arts))

        logger.info("News search: keyword='%s' entity='%s', kw=%d, entity=%d, used=%d",
                    keyword, entity, len(kw_articles), len(entity_articles), len(articles))

        news_context = _build_news_context(articles)

        # 카테고리별 프롬프트 선택
        use_fact = topic in _FACT_CATEGORIES or topic not in _EMPATHY_CATEGORIES
        base_prompt = _NAVER_PROMPT_FACT if use_fact else _NAVER_PROMPT_EMPATHY

        intent_guide = _INTENT_GUIDE.get(intent, _DEFAULT_INTENT_GUIDE)
        from datetime import date as _date
        today_str = _date.today().strftime("%Y년 %m월 %d일")
        prompt = base_prompt.format(
            keyword=keyword,
            category=topic,
            intent_guide=intent_guide,
            components_guide=_COMPONENTS_GUIDE,
            news_context=news_context,
            json_format=_JSON_FORMAT,
            today=today_str,
        )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=12288,
                system_instruction=(
                    f"당신은 한국어 블로그 작성 AI다. "
                    f"이번 포스팅의 주제는 반드시 '{keyword}'이어야 한다. "
                    f"제목과 본문 모두 '{keyword}'에 관한 내용만 작성한다. "
                    f"다른 주제는 절대 허용되지 않는다."
                ),
            ),
        )
        raw = response.text or ""

        try:
            data = _parse_json(raw)
        except Exception as e:
            logger.warning("Naver JSON parse failed (%s)", e)
            raise RuntimeError(f"Naver content parse failed: {e}")

        content = data.get("content", "")
        content = _BAD_OPENER.sub("", content).strip()
        content = _replace_component_labels(content)
        content = _fix_component_styles(content)
        content = _style_headings(content)
        if _REPEATED_ENDINGS.search(content):
            logger.warning("Repeated endings detected — keyword=%s", keyword)

        ts = data.get("traffic_score") or {}
        gemini_score = ts.get("score") if isinstance(ts, dict) else None
        competition   = ts.get("competition") if isinstance(ts, dict) else None
        trend_level   = ts.get("trend_level") if isinstance(ts, dict) else None
        score_reason  = ts.get("reason", []) if isinstance(ts, dict) else []

        logger.info(
            "Naver content generated — keyword=%s title=%s traffic=%s trend=%s",
            keyword, data.get("title", ""), gemini_score, trend_level,
        )
        return GeneratedContent(
            keyword=keyword,
            title=data.get("title", keyword),
            content=content,
            meta_description=data.get("meta_description", ""),
            tags=data.get("tags", []),
            summary=data.get("summary", []),
            language="ko",
            platform="naver",
            category=topic,
            gemini_traffic_score=int(gemini_score) if gemini_score is not None else None,
            competition=int(competition) if competition is not None else None,
            trend_level=str(trend_level) if trend_level else None,
            score_reason=score_reason if isinstance(score_reason, list) else [],
        )
