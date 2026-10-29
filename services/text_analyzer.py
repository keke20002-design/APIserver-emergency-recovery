import html
import re
import logging
from collections import Counter
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 혼잡 키워드 → 점수 가중치
CONGESTION_KEYWORDS: dict[str, int] = {
    "만차": 30, "헬": 20, "막힘": 20, "대기": 15,
    "혼잡": 20, "가득": 15, "꽉": 20, "정체": 15, "웨이팅": 15,
}

# 여유 키워드 → 점수 가중치
EASE_KEYWORDS: dict[str, int] = {
    "널널": 25, "여유": 20, "한산": 25,
    "빈자리": 15, "쉽게": 10, "금방": 10,
}

# 타입별 주요 키워드 (제목 선별 + 키워드 추출용)
TYPE_KEYWORDS: dict[str, list[str]] = {
    "parking":    ["주차", "주차장", "만차", "주차비"],
    "restaurant": ["맛집", "식당", "음식점", "카페", "레스토랑", "맛"],
    "food":       ["먹거리", "음식", "간식", "치킨", "맥주", "핫도그", "푸드"],
    "traffic":    ["교통", "버스", "지하철", "대중교통", "막힘", "정체"],
    "notice":     ["공지", "변경", "임시", "안내", "폐쇄", "휴무", "운영"],
    "tip":        ["팁", "꿀팁", "관람", "입장", "추천", "좌석", "후기"],
}

# 고정 summary 템플릿 (이모지 포함)
SUMMARY_TEMPLATES: dict[str, dict[str, str]] = {
    "parking": {
        "high": "🚗 주차 혼잡 예상",
        "mid":  "🚗 주차 보통",
        "low":  "🚗 주차 여유",
    },
    "restaurant": {
        "high": "🍽️ 맛집 웨이팅 있음",
        "mid":  "🍽️ 맛집 보통",
        "low":  "🍽️ 맛집 여유",
    },
    "food": {
        "high": "🍗 외야 먹거리 인기",
        "mid":  "🍗 먹거리 보통",
        "low":  "🍗 먹거리 여유",
    },
    "traffic": {
        "high": "🚇 경기장 주변 교통 혼잡",
        "mid":  "🚇 교통 보통",
        "low":  "🚇 교통 원활",
    },
    "notice": {
        "high": "📢 중요 공지",
        "mid":  "📢 공지사항",
        "low":  "📢 공지사항",
    },
    "tip": {
        "high": "💡 관람 팁",
        "mid":  "💡 관람 팁",
        "low":  "💡 관람 팁",
    },
}

_TRIM_PREFIX = re.compile(r"^(re:?\s*|RE:?\s*|\[.*?\]\s*)+", re.IGNORECASE)
# 한국어 불용어 (키워드 추출 시 제외)
_STOPWORDS = {
    "이", "가", "을", "를", "의", "에", "에서", "로", "으로", "와", "과",
    "은", "는", "도", "만", "까지", "부터", "이다", "있다", "없다", "하다",
    "그", "저", "이것", "저것", "그것", "때", "것", "수", "등", "및",
    "야구장", "야구", "경기", "오늘", "어제", "내일",
}


def _unescape(text: str) -> str:
    return html.unescape(text)


def _clean_title(title: str) -> str:
    title = _unescape(title)
    title = _TRIM_PREFIX.sub("", title).strip()
    return title[:60]


def _score_to_level(score: int) -> str:
    if score >= 68:
        return "high"
    elif score >= 38:
        return "mid"
    return "low"


def _calculate_score(texts: list[str]) -> int:
    if not texts:
        return 50
    combined = " ".join(texts)
    score = 50
    for kw, pts in CONGESTION_KEYWORDS.items():
        score += pts * min(combined.count(kw), 3)
    for kw, pts in EASE_KEYWORDS.items():
        score -= pts * min(combined.count(kw), 3)
    return max(0, min(100, score))


def _calculate_confidence(score: int, text_count: int) -> float:
    """score와 수집된 텍스트 수 기반 신뢰도 계산 (0.0~1.0)."""
    base = abs(score - 50) / 50        # 기본값 50에서 멀수록 신뢰도 높음
    coverage = min(text_count / 5, 1.0) # 5개 이상 수집 시 최대
    return round(min(base * 0.6 + coverage * 0.4, 1.0), 2)


def _extract_keywords(texts: list[str], titles: list[str], update_type: str) -> str:
    """타입 관련 키워드 빈도 추출 → 쉼표 구분 문자열 반환."""
    combined = " ".join(titles + texts)
    combined = _unescape(combined)
    # 2글자 이상 한국어 단어 추출
    words = re.findall(r"[가-힣]{2,}", combined)
    counter = Counter(w for w in words if w not in _STOPWORDS)
    # 타입 키워드 포함 상위 5개 우선
    type_kws = set(TYPE_KEYWORDS.get(update_type, []))
    ranked = sorted(counter.items(), key=lambda x: (x[0] in type_kws, x[1]), reverse=True)
    return ", ".join(w for w, _ in ranked[:5])


def _best_title_with_link(
    titles: list[str], links: list[str], update_type: str
) -> tuple[str, str | None] | None:
    """가장 관련도 높은 제목과 해당 URL을 함께 반환."""
    keywords = TYPE_KEYWORDS.get(update_type, [])
    all_signal = list(CONGESTION_KEYWORDS) + list(EASE_KEYWORDS)
    paired = list(zip(titles, links + [None] * len(titles)))  # links 부족 시 None 패딩

    for title, link in paired:
        if any(kw in title for kw in keywords) and any(kw in title for kw in all_signal):
            return _clean_title(title), link
    for title, link in paired:
        if any(kw in title for kw in keywords):
            return _clean_title(title), link
    for title, link in paired:
        cleaned = _clean_title(title)
        if 5 <= len(cleaned) <= 60:
            return cleaned, link
    return None


def _extract_best_sentence(texts: list[str], update_type: str) -> str:
    combined = _unescape(" ".join(texts))
    keywords = TYPE_KEYWORDS.get(update_type, [])
    for sentence in re.split(r"[.!?。\n]", combined):
        sentence = sentence.strip()
        if 8 <= len(sentence) <= 50 and any(kw in sentence for kw in keywords):
            return sentence
    return ""


def analyze_update(
    stadium: str,
    update_type: str,
    texts: list[str],
    titles: list[str] | None = None,
    links: list[str] | None = None,
) -> dict | None:
    """
    수집 텍스트를 분석해 Supabase 저장용 dict 반환.

    반환 필드:
    - message   : 블로그 원문 제목 (운영자 확인용)
    - raw_text  : 수집된 텍스트 원문 요약 (최대 500자)
    - summary   : 이모지 + 고정 템플릿 (앱 표시용)
    - keywords  : 추출된 키워드 (쉼표 구분)
    - confidence: 신뢰도 0.0~1.0
    - source    : 데이터 출처
    - score     : 혼잡도 점수 0~100
    """
    titles = titles or []
    links = links or []

    if not texts and not titles:
        return None

    score = _calculate_score(texts)
    level = _score_to_level(score)

    # 운영자 확인용 message = 실제 블로그 제목
    best = _best_title_with_link(titles, links, update_type)
    message = best[0] if best else _extract_best_sentence(texts, update_type)
    source_url = best[1] if best else None
    if not message:
        logger.debug("No meaningful content for %s/%s — skipped", stadium, update_type)
        return None

    # 앱 표시용 summary = 고정 템플릿
    summary = SUMMARY_TEMPLATES.get(update_type, {}).get(level, message)[:50]

    # raw_text = 원문 500자 이내
    raw_text = _unescape(" ".join(texts))[:500]

    keywords = _extract_keywords(texts, titles, update_type)
    confidence = _calculate_confidence(score, len(texts))

    return {
        "stadium": stadium,
        "type": update_type,
        "message": message,
        "raw_text": raw_text,
        "summary": summary,
        "keywords": keywords,
        "confidence": confidence,
        "source": "naver_blog",
        "source_url": source_url,
        "score": score,
        "status": "pending",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
