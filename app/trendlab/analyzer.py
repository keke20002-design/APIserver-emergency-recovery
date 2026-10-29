"""TrendLab Analyzer v2 — Event Clustering + 다차원 점수.

TrendScore(얼마나 뜨는가)와 BlogScore(블로그로 쓸 가치가 있는가)는 완전히 다른 개념이다
(scoring.py). 이 모듈은 Trend 기준으로 후보를 뽑고 정렬하는 기존 로직은 그대로 두되,
그 위에 Source Relevance Gate + BlogScore + Topic Purity + Article Eligibility +
Topic Deduplication을 추가로 적용해 최종 "Blog TOP10"을 만든다. Trend 기준으로만 뽑은
목록은 관리자 디버깅용으로 별도 보관한다(get_last_trend_debug)."""
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone

from app.trendlab import freshness, scoring
from app.trendlab.relevance import gate_sources

logger = logging.getLogger(__name__)

# 관리자 디버깅용 — 마지막 extract_topics() 호출의 Trend 기준 후보 목록(dedup/eligibility
# 필터링 이전). Blog TOP10에서는 빠졌지만 "지금 뭐가 뜨고 있었는지"를 보고 싶을 때 쓴다.
_last_trend_debug: list[dict] = []


def get_last_trend_debug() -> list[dict]:
    return [{k: v for k, v in t.items() if k != "source_items"} for t in _last_trend_debug]

# ── 언론사/채널 블랙리스트 ────────────────────────────────────────────────────
MEDIA_BLACKLIST: set[str] = {
    "KBS", "SBS", "MBC", "JTBC", "YTN", "MBN", "TV조선", "채널A", "OBS",
    "연합뉴스", "연합뉴스TV", "뉴스1", "뉴시스", "뉴스타파", "한겨레",
    "조선일보", "중앙일보", "동아일보", "매일경제", "한국경제", "이데일리",
    "파이낸셜뉴스", "머니투데이", "헤럴드경제", "아시아경제", "서울경제",
    "국민일보", "세계일보", "문화일보", "경향신문", "오마이뉴스",
    "CNN", "BBC", "Fox News", "Reuters", "Bloomberg", "AP", "AFP",
    "NBC", "ABC", "CBS", "MSNBC", "NYT",
    "슈카월드", "침착맨", "KBS뉴스", "SBS뉴스", "MBC뉴스", "JTBC뉴스",
}

_STOP_WORDS = {
    "오늘", "지금", "최근", "이번", "현재", "기자", "뉴스", "속보",
    "단독", "공식", "확인", "발표", "예정", "관련", "통해", "위해",
}


def is_media_outlet(name: str) -> bool:
    n = name.strip()
    return (
        n in MEDIA_BLACKLIST
        or any(n.startswith(m) for m in MEDIA_BLACKLIST)
        or n.endswith(("뉴스", "방송", "신문", "채널", "TV", "News"))
    )


# ── Click Score 키워드 ────────────────────────────────────────────────────────
_CLICK_WORDS: dict[str, int] = {
    "열애": 15, "결혼": 12, "이혼": 12, "논란": 10, "폭로": 15,
    "사망": 15, "은퇴": 12, "우승": 10, "신기록": 12,
    "급등": 12, "급락": 12, "역전": 10, "컴백": 12,
    "업데이트": 8, "출시": 10, "IPO": 10, "AI": 5,
    "ChatGPT": 8, "비트코인": 10, "ETF": 8,
    "신곡": 8, "월드투어": 10, "고소": 10, "구속": 12,
    "파산": 12, "해고": 8, "충돌": 8, "사고": 8, "화재": 10,
    "폭락": 12, "신작": 8, "협박": 12, "성범죄": 15, "탈출": 10,
    "결별": 12, "재결합": 12, "근황": 7, "비하인드": 8, "반전": 8,
    "눈물": 7, "최초": 8, "실물": 7, "갑질": 12,
}

# ── Blog Value 힌트 ───────────────────────────────────────────────────────────
_BV_HIGH = {"AI", "기술", "투자", "ETF", "방법", "비교", "순위", "추천", "분석", "전망", "가이드"}
_BV_MED  = {"업데이트", "출시", "발표", "정리", "총정리", "이유", "설명", "차이"}
_BV_LOW  = {"루머", "소문", "찌라시", "카더라"}
_CAT_BV  = {"IT": 20, "경제": 18, "게임": 15, "교육": 20, "스포츠": 8, "연예": 5, "사회": 12, "정치": 10}

# BlogScore의 정보성 평가는 유지하면서 TrendLab의 편집 방향을 최종 순위에 반영한다.
# 정치·사회는 삭제하지 않고, 강한 트렌드 신호가 있을 때만 경쟁하도록 약하게 감점한다.
_EDITORIAL_CATEGORY_BONUS = {
    "연예": 15, "스포츠": 5, "게임": 3, "IT": 0,
    "경제": -4, "사회": -8, "정치": -8, "교육": -4,
}


def _editorial_bonus(category: str, click_score: int) -> int:
    """연예 중심 편집 방향과 실제 제목의 클릭 관심 신호를 보정한다."""
    category_bonus = _EDITORIAL_CATEGORY_BONUS.get(category, 0)
    # 단어 수에 따른 과대평가를 막기 위해 클릭 보정은 10점으로 상한을 둔다.
    click_bonus = min(10, max(0, click_score) // 10)
    return category_bonus + click_bonus


def _click_score(topic: str) -> int:
    return min(100, sum(pts for w, pts in _CLICK_WORDS.items() if w in topic))


def _blog_value(topic: str, category: str) -> int:
    base = 50
    for w in _BV_HIGH:
        if w in topic: base += 10
    for w in _BV_MED:
        if w in topic: base += 5
    for w in _BV_LOW:
        if w in topic: base -= 15
    base += _CAT_BV.get(category, 10)
    return max(0, min(100, base))


def _view_score(total_views: int) -> int:
    """Log-based 조회수 점수 (plan.md 표 기준)."""
    if total_views >= 10_000_000: return 20
    if total_views >=  5_000_000: return 17
    if total_views >=  1_000_000: return 12
    if total_views >=    500_000: return 9
    if total_views >=    100_000: return 5
    if total_views >=     30_000: return 2
    return 0


def _is_novel(topic: str) -> bool:
    """최근 7일 내 같은 키워드 포스트 없으면 True (신규성 +15)."""
    try:
        from storage.post_repository import PostRepository
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        repo = PostRepository()
        words = [w for w in topic.split() if len(w) >= 2]
        if not words:
            return True
        with repo._conn() as conn:
            for word in words:
                row = conn.execute(
                    "SELECT 1 FROM blog_posts WHERE keyword LIKE ? AND created_at > ? LIMIT 1",
                    (f"%{word}%", cutoff),
                ).fetchone()
                if row:
                    return False
    except Exception:
        pass
    return True


def _build_reason(
    naver_count: int,
    yt_count: int,
    source_diversity: int,
    total_views: int,
    uploaded_today: bool,
    is_novel: bool,
) -> str:
    """데이터 기반 추천 이유 — Gemini 사용 안 함."""
    parts = []
    if naver_count > 0:
        parts.append(f"뉴스 {naver_count}건")
    if yt_count > 0:
        parts.append(f"유튜브 {yt_count}건")
    if source_diversity > 1:
        parts.append(f"언론사 {source_diversity}곳")
    if total_views >= 10_000:
        if total_views >= 1_000_000:
            parts.append(f"조회수 {total_views//10000}만")
        else:
            parts.append(f"조회수 {total_views//1000}천")
    if uploaded_today:
        parts.append("오늘 업로드")
    if is_novel:
        parts.append("오늘 첫 등장")
    return " · ".join(parts) if parts else ""


def _topic_matches(topic: str, title: str) -> bool:
    """topic 문구의 단어 대부분이 title에 있어야 매칭 — 단어 하나만 겹쳐도 매칭되던
    이전 방식(any())은 "2026", "발표" 같은 흔한 단어 하나 때문에 전혀 무관한 기사가
    topic의 source_items로 묶이는 사고를 냈다 (예: "IFA 2026 클로징"이 "2026"만 겹치는
    "OO 아시안게임 2026" 기사와 매칭). 단어 대부분이 겹치고, 그중 최소 하나는 숫자가
    아닌 핵심 단어여야 매칭으로 인정한다."""
    words = [w for w in topic.split() if len(w) >= 2]
    if not words:
        return topic in title
    hits = sum(1 for w in words if w in title)
    if hits < max(1, len(words) - 1):
        return False
    non_numeric = [w for w in words if not w.isdigit()]
    return not non_numeric or any(w in title for w in non_numeric)


# ── 핵심: Event Clustering NER ───────────────────────────────────────────────

async def _ner_event_call(items: list[dict], source_label: str) -> dict[str, dict]:
    """
    제목 목록 → Gemini에게 각 제목 독립 분석 요청
    → 실제 co-occurrence 기반 Event Topic 추출.
    """
    from google import genai
    from google.genai import types

    if not items:
        return {}

    api_key = os.getenv("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)

    now = datetime.now(timezone.utc)
    kst = timezone(timedelta(hours=9))

    def _fmt(i: dict) -> str | None:
        title = i.get("title")
        if not title:
            return None
        pub_dt = freshness.parse_to_utc(i.get("published_at"))
        stamp = pub_dt.astimezone(kst).strftime("%Y-%m-%d %H:%M") if pub_dt else "날짜 미상"
        return f"- [{stamp}] {title}"

    title_block = "\n".join(f for i in items[:200] if (f := _fmt(i)))

    prompt = (
        f"아래는 한국 {source_label} 제목 목록입니다 (각 줄 앞 [YYYY-MM-DD HH:MM]은 게재 시각, 한국시간).\n"
        f"현재 시각(한국시간): {now.astimezone(kst).strftime('%Y-%m-%d %H:%M')}\n\n"
        "오늘 한국에서 실제로 진행 중인 '사건(Event)'을 추출하세요 — 오래된 사건을 새로운 사건인 것처럼\n"
        "묶지 마세요. 기사 날짜가 오래됐다면(예: 여러 날 전) 그 사건은 현재 이슈로 판단하지 않습니다.\n\n"
        "⚠️ 핵심 규칙 — 반드시 준수:\n"
        "1. 각 제목을 독립적으로 분석 — 다른 제목의 단어와 절대 합치지 마세요\n"
        "   나쁜 예: '배틀그라운드 멸망전' (배틀그라운드/멸망전이 서로 다른 제목에 있음)\n"
        "   좋은 예: 'LOL 멸망전' (같은 제목 안에 LOL과 멸망전이 함께 있음)\n"
        "2. 제목에 실제로 존재하는 단어만 사용 — 없는 단어 조합 금지\n"
        "3. 반드시 [주체 + 사건] 형태:\n"
        "   나쁜 예: '리센느', '손흥민', '메이플' (주체만 있음)\n"
        "   좋은 예: '리센느 컴백', '손흥민 이적설', '메이플 여름 업데이트'\n"
        "4. 방송사·언론사·채널명은 절대 포함 금지 (KBS, SBS, MBC, 연합뉴스, 슈카월드 등)\n"
        "   → '[KBS] LOL 멸망전' → Topic은 'LOL 멸망전', KBS는 버림\n"
        "5. 같은 제목에서 co-occur하는 단어로만 구문 형성\n"
        "6. 최소 2개 이상 제목에서 확인된 사건만 포함\n"
        "7. 비슷한 사건은 하나의 Topic으로 통합\n"
        "8. 날짜가 오래된 제목들끼리만 co-occur하는 사건은 제외하세요 — 최근(가능하면 24시간 이내)\n"
        "   게재된 제목이 최소 1개 이상 포함된 사건만 추출합니다.\n\n"
        "출력: JSON 배열만, 설명 금지\n"
        '[{"event": "사건명", "count": 등장횟수, '
        '"category": "연예/스포츠/경제/정치/게임/IT/사회/교육 중 1개", '
        '"example_title": "대표 원문 제목"}]\n\n'
        f"제목 목록:\n{title_block}"
    )

    response_schema = types.Schema(
        type=types.Type.ARRAY,
        items=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "event": types.Schema(type=types.Type.STRING),
                "count": types.Schema(type=types.Type.INTEGER),
                "category": types.Schema(
                    type=types.Type.STRING,
                    enum=["연예", "스포츠", "경제", "정치", "게임", "IT", "사회", "교육"],
                ),
                "example_title": types.Schema(type=types.Type.STRING),
            },
            required=["event", "count", "category", "example_title"],
        ),
    )

    parsed: list = []
    for attempt in range(2):
        raw = ""
        try:
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0.1,
                    max_output_tokens=16384,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            raw = response.text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            m = re.search(r"\[[\s\S]*\]", raw)
            parsed = json.loads(m.group() if m else raw)
            break
        except Exception as e:
            logger.warning(
                "NER failed (%s) attempt %d: %s | raw=%s",
                source_label, attempt + 1, e, raw[:200],
            )
            parsed = []

    result: dict[str, dict] = {}
    for info in parsed:
        if not isinstance(info, dict):
            continue
        name = str(info.get("event", "")).strip()
        if len(name) < 2 or name in _STOP_WORDS or is_media_outlet(name):
            continue
        result[name] = {
            "count": max(1, int(info.get("count", 1))),
            "category": info.get("category") or "기타",
            "example_title": info.get("example_title", ""),
        }

    logger.info("NER [%s]: %d events extracted", source_label, len(result))
    return result


async def _validate_topic(topic: str, example_title: str) -> bool:
    """Gemini: 실제 존재하는 사건인가? 최종 검증."""
    from google import genai
    from google.genai import types
    api_key = os.getenv("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)
    prompt = (
        f"Topic: '{topic}'\n"
        f"근거 제목: '{example_title}'\n\n"
        "이 Topic이 일반 대중이 클릭할 만한 실제로 존재하는 사건인지 판단하세요.\n"
        "다음 조건을 모두 만족해야 valid=true입니다:\n"
        "1. 다른 기사의 단어를 조합해 만들어진 것이 아닌, 실제 사건이어야 한다.\n"
        "2. Topic 문구에 결과·상태를 단정하는 표현(구속, 사망, 확정, 체포, 해고, 파산, 승인,"
        " 퇴출, 폐지, 발표 등)이 들어있다면, 근거 제목이 그 결과를 실제로 확정된 사실로 보도하는지 확인하라."
        " 근거 제목이 '기각', '불구속', '무혐의', '부인', '반박', '검토 중', '~여부',"
        " '~촉구', '~주장', '~요구', '미확인', '~예정', '~가능성'처럼 아직 확정되지 않았거나"
        " 부정된 내용을 담고 있는데 Topic이 그 결과를 이미 일어난 사실처럼 단정하고 있다면 invalid로 판단하라.\n"
        "   나쁜 예: 근거 제목이 '구속영장 기각'인데 Topic이 '~구속' (안 됐는데 된 것처럼 단정)\n"
        "   좋은 예: 근거 제목이 '구속영장 기각'이고 Topic도 '~구속영장 기각' 또는 '~구속 여부 논란'\n"
        "3. Topic이나 근거 제목에 실제 인물·브랜드의 구체적인 이름 대신 '세계 1위 유튜버',"
        " '유명 연예인', '톱스타', 'A씨' 같은 모호한 지칭만 있고 구체적 사실(누가, 무엇을, 언제)이"
        " 확인되지 않는다면 invalid로 판단하라 — 이런 경우 실제 보도가 아니라 온라인 추측·루머일 가능성이 높다.\n"
        "   나쁜 예: Topic '세계 1위 유튜버 결혼 계약서' — 누구인지, 계약서에 뭐가 적혔는지 특정 안 됨\n\n"
        '{"valid": true 또는 false, "reason": "한 줄 설명"}'
    )
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
                max_output_tokens=512,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        valid = bool(parsed.get("valid", True))
        logger.info("Validate '%s': valid=%s reason=%s", topic, valid, parsed.get("reason", ""))
        return valid
    except Exception as e:
        logger.warning("Validate failed for '%s': %s", topic, e)
        return True  # 실패 시 통과


def _compute_virality(
    naver_count: int,
    yt_count: int,
    source_diversity: int,
    view_pts: int,
    uploaded_today: bool,
    is_novel: bool,
) -> int:
    base = naver_count * 1.5 + yt_count * 3.0
    base += (source_diversity - 1) * 3  # 언론사 다양성 보너스
    if uploaded_today: base *= 1.2
    if is_novel: base += 15
    base += view_pts
    return int(min(99, base))


# ── 신선도 방어 로직 (분석 단계) ─────────────────────────────────────────────
# collector.py가 이미 published_at 없는/미래/7일 초과 아이템을 걸러내지만,
# analyzer는 collector를 거치지 않은 입력(테스트, 향후 다른 소스)에도 안전하도록
# 스스로도 동일한 필터를 한 번 더 적용한다 (수집 단계와는 독립적인 방어선).

def _filter_collectible_items(items: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    return [
        i for i in items
        if freshness.is_collectible(freshness.parse_to_utc(i.get("published_at")), now)
    ]


def _trim_source_items(items: list[dict], max_items: int = 15) -> list[dict]:
    """Topic에 묶인 원본 기사/영상 목록을 생성 단계(사실/주장 분리)에 넘길 수 있도록
    필요한 필드만 남기고 최신순으로 자른다."""
    def _key(i: dict) -> str:
        return i.get("published_at") or ""
    ordered = sorted(items, key=_key, reverse=True)[:max_items]
    return [
        {
            "source": i.get("source", ""),
            "title": i.get("title", ""),
            "description": i.get("description", ""),
            "published_at": i.get("published_at"),
            "publisher": i.get("publisher", ""),
            "url": i.get("url", ""),
        }
        for i in ordered
    ]


def _source_summary(matched_items: list[dict], now: datetime | None = None) -> dict:
    """Topic에 연결된 source_items(matched_naver + matched_yt)로부터
    recent_source_count(48시간 이내) / old_source_count(48시간~7일) / latest_published_at을 계산."""
    now = now or datetime.now(timezone.utc)
    recent = old = 0
    latest: datetime | None = None
    for i in matched_items:
        pub_dt = freshness.parse_to_utc(i.get("published_at"))
        if pub_dt is None:
            continue
        if freshness.is_recent(pub_dt, now):
            recent += 1
        elif freshness.classify(pub_dt, now) == "aged7d":
            old += 1
        if latest is None or pub_dt > latest:
            latest = pub_dt
    return {
        "recent_source_count": recent,
        "old_source_count": old,
        "latest_published_at": latest.isoformat() if latest else None,
        "freshness_label": freshness.freshness_label(latest, now) if latest else "",
        "_latest_dt": latest,
    }


# ── 메인 진입점 ───────────────────────────────────────────────────────────────

async def extract_topics(items: list[dict]) -> list[dict]:
    """
    수집 아이템 → Event Clustering → 다차원 점수 → TOP10.
    items: list[dict] with keys: source, title, description, view_count, published_at, uploaded_today, publisher

    날짜 방어 로직 3단계:
      1) 여기(분석 단계 진입 시) — published_at 없음/미래/7일 초과 아이템 재필터
      2) Topic별 virality에 freshness multiplier 적용 (최신 source일수록 가중)
      3) TOP10 확정 직전 — 48시간 이내 source가 하나도 없는 Topic은 최종 제외
    """
    import asyncio

    now = datetime.now(timezone.utc)
    items = _filter_collectible_items(items, now)

    naver_items = [i for i in items if i["source"] == "naver"]
    yt_items    = [i for i in items if i["source"].startswith("youtube")]

    naver_ents, yt_ents = await asyncio.gather(
        _ner_event_call(naver_items, "네이버 뉴스"),
        _ner_event_call(yt_items, "YouTube"),
    )

    all_topics = set(naver_ents) | set(yt_ents)
    results: list[dict] = []

    for topic in all_topics:
        if is_media_outlet(topic):
            logger.info("Skipping media: '%s'", topic)
            continue

        n_info = naver_ents.get(topic, {})
        y_info = yt_ents.get(topic, {})
        naver_count = n_info.get("count", 0)
        yt_count    = y_info.get("count", 0)
        category    = n_info.get("category") or y_info.get("category") or "기타"
        example     = n_info.get("example_title") or y_info.get("example_title") or ""

        # 원본 아이템 매칭
        matched_naver = [i for i in naver_items if _topic_matches(topic, i.get("title", ""))]
        matched_yt    = [i for i in yt_items    if _topic_matches(topic, i.get("title", ""))]
        source_items  = matched_naver + matched_yt

        # 출처가 없는 Topic은 생성하지 않는다 (Gemini가 클러스터링만 하고 실제 원문을
        # 다시 찾지 못한 경우 — 근거 없는 Topic이 TOP10에 오르는 것을 막는다)
        if not source_items:
            logger.info("Skipping topic with no matched source items: '%s'", topic)
            continue

        # Source Diversity: distinct publisher domains
        publishers = {i.get("publisher", "") for i in matched_naver if i.get("publisher")}
        source_diversity = max(1, len(publishers))

        total_views  = sum(i.get("view_count", 0) for i in matched_yt)
        uploaded_today = any(i.get("uploaded_today", False) for i in matched_yt)

        sources: list[str] = []
        if naver_count > 0: sources.append("naver")
        for s in set(i["source"] for i in matched_yt):
            if s not in sources: sources.append(s)

        novel    = _is_novel(topic)
        view_pts = _view_score(total_views)
        virality = _compute_virality(naver_count, yt_count, source_diversity, view_pts, uploaded_today, novel)
        click    = _click_score(topic)
        bv       = _blog_value(topic, category)
        reason   = _build_reason(naver_count, yt_count, source_diversity, total_views, uploaded_today, novel)

        # 신선도 가중 — 이 Topic의 가장 최근 source가 얼마나 최신인지에 따라 virality를 보정.
        # "오래된 기사 하나만 있는 Topic"과 "지금 막 새 기사가 붙은 Topic"을 점수로 구분한다.
        summary = _source_summary(source_items, now)
        fresh_mult = freshness.freshness_multiplier(summary["_latest_dt"], now)
        virality = int(min(99, virality * fresh_mult))

        # ── Source Relevance Gate + BlogScore + Topic Purity + Eligibility ──────
        # TrendScore(virality)가 아무리 높아도 관련 source가 부족하면 article을 만들
        # 가치가 없다 — 이 판단은 여기서 미리 끝내둔다. Article Generator에는 이 gate를
        # 통과한 source만 전달한다(source_items를 gate.relevant_sources로 교체).
        gate = gate_sources(topic, source_items)
        blog_score_result = scoring.compute_blog_score(
            topic=topic, gate=gate,
            naver_count=naver_count, yt_count=yt_count,
            source_diversity=source_diversity, total_views=total_views,
            is_novel=novel, now=now,
        )
        topic_purity, purity_reason = scoring.compute_topic_purity_score(gate)
        article_eligible, block_reason = scoring.compute_article_eligibility(gate, topic_purity)

        results.append({
            "topic":                topic,
            "category":             category,
            "virality":             virality,
            "click_score":          click,
            "blog_value":           bv,
            "naver_count":          naver_count,
            "yt_count":             yt_count,
            "source_diversity":     source_diversity,
            "total_views":          total_views,
            "uploaded_today":       uploaded_today,
            "is_novel":             novel,
            "reason":               reason,
            "example_title":        example,
            "sources":              sources,
            "recent_source_count":  summary["recent_source_count"],
            "old_source_count":     summary["old_source_count"],
            "latest_published_at":  summary["latest_published_at"],
            "freshness_label":      summary["freshness_label"],
            # Blog TOP10 후보 판단에 쓰이는 신규 필드 — virality/blog_value는 하위호환을 위해 유지.
            "total_source_count":            gate.source_count,
            "relevant_source_count":         gate.relevant_source_count,
            "strong_relevant_source_count":  gate.strong_relevant_source_count,
            "average_relevance":             round(gate.average_relevance, 1),
            "latest_relevant_source":        gate.latest_relevant_source,
            "topic_purity_score":            topic_purity,
            "topic_purity_reason":           purity_reason,
            "article_eligible":              article_eligible,
            "article_block_reason":          block_reason,
            **blog_score_result.to_dict(),
            # Article Generator에는 gate를 통과한 source만 넘긴다 — 무관한 기사가
            # 프롬프트에 섞여 "오늘의 다른 뉴스"로 분량을 채우는 것을 막는다.
            "source_items":         _trim_source_items(gate.relevant_sources),
        })

    if not results:
        return []

    # Virality(TrendScore) 기준 정렬 → 정규화 → TOP10 후보. FinalScore는 정규화된
    # TrendScore를 써야 하므로 정규화 이후에 계산한다.
    results.sort(key=lambda x: -x["virality"])
    max_v = results[0]["virality"] or 1
    for r in results:
        r["virality"] = min(99, int(r["virality"] / max_v * 99)) if max_v < 99 else r["virality"]
        r["trend_score"] = r["virality"]
        r["editorial_bonus"] = _editorial_bonus(r["category"], r["click_score"])
        r["final_score"] = (
            scoring.compute_final_score(
                r["trend_score"], r["blog_score"], r["editorial_bonus"]
            )
            if r["article_eligible"] else None
        )

    # TOP15 중 Gemini 검증 통과한 것만 TOP10으로 추림
    validated: list[dict] = []
    validate_tasks = [_validate_topic(r["topic"], r["example_title"]) for r in results[:15]]
    valid_flags = await asyncio.gather(*validate_tasks, return_exceptions=True)

    for r, flag in zip(results[:15], valid_flags):
        if flag is True or isinstance(flag, Exception):
            validated.append(r)
        else:
            logger.info("Filtered out invalid topic: '%s'", r["topic"])
        if len(validated) >= 10:
            break

    # ── 최종 안전장치 ────────────────────────────────────────────────────────
    # 48시간 이내 source가 하나도 없는 Topic은 TOP10 반환 직전에 제거한다.
    # ("현재 실제로 화제가 되고 있는 Topic"만 남기기 위한 마지막 방어선 —
    #  오래된 기사 하나만으로 만들어진 Topic이 여기서 걸러진다.)
    final: list[dict] = []
    for r in validated:
        if r["recent_source_count"] < 1:
            logger.info(
                "Filtered out stale topic (no source within 48h): '%s' (old_source_count=%d, latest=%s)",
                r["topic"], r["old_source_count"], r["latest_published_at"],
            )
            continue
        final.append(r)

    for i, r in enumerate(final):
        r["trend_rank"] = i + 1

    global _last_trend_debug
    _last_trend_debug = list(final)  # Trend 기준 순서 그대로 — 관리자 디버깅용 스냅샷

    # ── Topic Deduplication ──────────────────────────────────────────────────
    # "취업자 수 증가"/"청년 고용"처럼 의미가 겹치는 topic이 TOP10을 동시에 차지하지
    # 않도록 cluster를 부여하고, cluster당 상위 1개(기본값)만 살린다.
    clustered = await scoring.cluster_topics(final)

    # ── Blog TOP10 조립 ──────────────────────────────────────────────────────
    # "뜨는 주제"가 아니라 "블로그로 쓸 가치가 있는 주제"를 최종 TOP10으로 삼는다 —
    # Virality가 99여도 article_eligible=False(관련 source 부족 등)이면 제외된다.
    blog_top10 = [r for r in clustered if r.get("article_eligible") and not r.get("is_duplicate")]
    blog_top10.sort(key=lambda r: -(r.get("final_score") or 0))
    blog_top10 = blog_top10[:10]
    for i, r in enumerate(blog_top10):
        r["rank"] = i + 1

    logger.info(
        "TrendLab TOP10: %d trend candidates → %d blog-eligible (dedup + eligibility applied)",
        len(final), len(blog_top10),
    )

    return blog_top10
