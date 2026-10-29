"""TrendLab Scoring — TrendScore(지금 얼마나 뜨는가)와 BlogScore(블로그로 쓸 가치가
있는가)를 완전히 분리해서 계산한다.

    Source-Topic Matching → Relevance Gate → Topic Deduplication →
    TrendScore → BlogScore → Article Eligibility → FinalScore → TOP10

기존 virality/blog_value 필드는 그대로 유지한다(하위호환) — 이 모듈은 그 위에
trend_score(=virality 별칭), blog_score(신규 100점 breakdown), topic_purity_score,
article_eligible/article_block_reason, final_score를 추가로 계산해 붙인다.

핵심 원칙: "뜨는 주제"와 "블로그로 쓸 만한 주제"는 다른 개념이다. Virality가 99여도
관련 source가 없으면(Relevance 0) BlogScore가 낮고, article_eligible=False가 되어
FinalScore 계산 전에 Blog TOP10에서 제외된다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.trendlab import freshness
from app.trendlab.relevance import RelevanceGateResult, _tokenize

logger = logging.getLogger(__name__)

# ── Config (조정 가능) ────────────────────────────────────────────────────────
MIN_TOPIC_PURITY = 80
TREND_WEIGHT = 0.4
BLOG_WEIGHT = 0.6
MAX_TOPICS_PER_CLUSTER = 1
CLUSTER_SIMILARITY_THRESHOLD = 0.5  # 키워드 기반 fallback 클러스터링에서만 사용

BLOCK_REASONS = (
    "NO_RELEVANT_SOURCES", "INSUFFICIENT_RELEVANT_SOURCES", "LOW_SOURCE_RELEVANCE",
    "LOW_TOPIC_PURITY", "LOW_BLOG_SCORE", "STALE_TOPIC", "DUPLICATE_TOPIC", "UNSUPPORTED_TOPIC",
)


# ── BlogScore 하위 요소들 ─────────────────────────────────────────────────────
# 실제 검색량/클릭 데이터 소스가 없으므로(요청사항 15, 18) 존재하지 않는 수치를
# 만들어내지 않는다 — 모두 지금 이 시스템이 실제로 갖고 있는 신호(제목 텍스트,
# 기사 수, 조회수, 발행 시각, 과거 포스팅 이력)만 사용하는 결정적 휴리스틱이다.

_SEARCH_INTENT_CONCRETE_HINTS = {
    "발표", "출시", "공개", "개막", "폐막", "축제", "지원", "결과", "순위",
    "일정", "장소", "가격", "신제품", "업데이트", "개통", "개장", "선정", "행사",
}
_SEARCH_INTENT_VAGUE_WORDS = {
    "오늘의", "좋은", "이슈", "화제", "소식", "다양한", "여러", "관심", "핫이슈",
}


def compute_search_intent(topic: str) -> tuple[int, str]:
    """검색 의도가 명확한가(0~20) — 실제 검색량 데이터 없이 topic 문구의 구체성만 본다.
    검색량 수치는 절대 만들어내지 않는다."""
    score = 10
    reasons = []
    if any(h in topic for h in _SEARCH_INTENT_CONCRETE_HINTS):
        score += 6
        reasons.append("구체적 이벤트/결과 키워드 포함")
    if any(ch.isdigit() for ch in topic):
        score += 2
        reasons.append("구체적 수치·연도 포함")
    if len(_tokenize(topic)) >= 2:
        score += 2
    if any(v in topic for v in _SEARCH_INTENT_VAGUE_WORDS):
        score -= 8
        reasons.append("뭉뚱그린 표현 포함 — 검색 의도 불명확")
    score = max(0, min(20, score))
    return score, ("; ".join(reasons) if reasons else "구체성 판단 근거 부족")


def compute_independent_source_count(relevant_sources: list[dict]) -> int:
    """제목 단어 유사도(Jaccard>=0.7)로 사실상 같은 보도자료 기반 기사를 하나로 묶는다.
    언론사 12곳이 같은 보도자료를 받아쓴 경우를 12개가 아니라 1개의 독립 source로 센다.
    (본문 전체 비교가 아니라 제목 기반 근사치임 — 완전한 fingerprint 비교는 아니다.)"""
    groups: list[set[str]] = []
    for s in relevant_sources:
        words = set(_tokenize(s.get("title", "")))
        if not words:
            continue
        for g in groups:
            overlap = len(words & g) / max(1, len(words | g))
            if overlap >= 0.7:
                g |= words
                break
        else:
            groups.append(words)
    return len(groups) if groups else len(relevant_sources)


def compute_information_richness(relevant_sources: list[dict]) -> tuple[int, str]:
    """단순히 source 개수가 아니라 독립적인 정보량을 본다 — 동일 보도자료 20건이
    20개의 독립 정보로 계산되지 않는다."""
    independent = compute_independent_source_count(relevant_sources)
    avg_desc_len = (
        sum(len(s.get("description", "") or "") for s in relevant_sources) / len(relevant_sources)
        if relevant_sources else 0
    )
    score = min(15, independent * 4 + (3 if avg_desc_len >= 30 else 0))
    reason = f"독립 source {independent}개, 평균 설명 길이 {int(avg_desc_len)}자"
    return int(score), reason


def compute_freshness_score(latest_relevant_source_iso: str | None, now: datetime | None = None) -> tuple[int, str]:
    """관련 없는 최신 뉴스가 있다고 topic이 최신이라 판단하지 않는다 — 반드시
    '관련' source 중 가장 최신 것(latest_relevant_source)만 기준으로 삼는다."""
    now = now or datetime.now(timezone.utc)
    dt = freshness.parse_to_utc(latest_relevant_source_iso)
    if dt is None:
        return 0, "관련 source의 발행 시각을 알 수 없음"
    hours = (now - dt).total_seconds() / 3600
    if hours <= 24:
        return 15, f"최신 관련 source가 {int(hours)}시간 전"
    if hours <= 48:
        return 12, f"최신 관련 source가 {int(hours)}시간 전"
    if hours <= 24 * 7:
        return 6, f"최신 관련 source가 {int(hours / 24)}일 전"
    return 0, "관련 source가 모두 7일 이전"


def compute_audience_interest(naver_count: int, yt_count: int, source_diversity: int, total_views: int) -> tuple[int, str]:
    """실제 신호(기사 수·조회수·언론사 다양성)만 사용 — 존재하지 않는 클릭/검색량
    데이터를 만들어내지 않는다."""
    score = 0
    if naver_count >= 3: score += 3
    elif naver_count >= 1: score += 1
    if yt_count >= 2: score += 2
    if source_diversity >= 3: score += 3
    elif source_diversity >= 2: score += 1
    if total_views >= 100_000: score += 2
    score = min(10, score)
    reason = f"뉴스 {naver_count}건 · 유튜브 {yt_count}건 · 언론사 {source_diversity}곳 · 조회수 {total_views}"
    return score, reason


def compute_content_depth(relevant_sources: list[dict]) -> tuple[int, str]:
    total_len = sum(len(s.get("description", "") or "") for s in relevant_sources)
    score = min(10, total_len // 60)  # 60자당 1점
    return int(score), f"관련 source 설명 총 {total_len}자"


def compute_competition(is_novel: bool) -> tuple[int, str]:
    if is_novel:
        return 5, "최근 7일 내 동일 주제 포스트 없음 — 경쟁 낮음"
    return 2, "최근 7일 내 유사 주제로 이미 포스팅함 — 경쟁 있음"


@dataclass
class BlogScoreResult:
    blog_score: int
    source_relevance: int
    search_intent: int
    information_richness: int
    freshness: int
    audience_interest: int
    content_depth: int
    competition: int
    reasons: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "blog_score": self.blog_score,
            "source_relevance": self.source_relevance,
            "search_intent": self.search_intent,
            "information_richness": self.information_richness,
            "freshness": self.freshness,
            "audience_interest": self.audience_interest,
            "content_depth": self.content_depth,
            "competition": self.competition,
            "blog_score_reasons": self.reasons,
        }


def compute_blog_score(
    *,
    topic: str,
    gate: RelevanceGateResult,
    naver_count: int,
    yt_count: int,
    source_diversity: int,
    total_views: int,
    is_novel: bool,
    now: datetime | None = None,
) -> BlogScoreResult:
    """100점 만점: Source Relevance 25 + Search Intent 20 + Information Richness 15
    + Freshness 15 + Audience Interest 10 + Content Depth 10 + Competition 5."""
    source_relevance_pts = round(gate.average_relevance / 100 * 25)
    search_intent_pts, search_intent_reason = compute_search_intent(topic)
    richness_pts, richness_reason = compute_information_richness(gate.relevant_sources)
    freshness_pts, freshness_reason = compute_freshness_score(gate.latest_relevant_source, now)
    audience_pts, audience_reason = compute_audience_interest(naver_count, yt_count, source_diversity, total_views)
    depth_pts, depth_reason = compute_content_depth(gate.relevant_sources)
    competition_pts, competition_reason = compute_competition(is_novel)

    total = (
        source_relevance_pts + search_intent_pts + richness_pts
        + freshness_pts + audience_pts + depth_pts + competition_pts
    )
    return BlogScoreResult(
        blog_score=max(0, min(100, total)),
        source_relevance=source_relevance_pts,
        search_intent=search_intent_pts,
        information_richness=richness_pts,
        freshness=freshness_pts,
        audience_interest=audience_pts,
        content_depth=depth_pts,
        competition=competition_pts,
        reasons={
            "source_relevance": f"관련 source 평균 관련성 {gate.average_relevance:.0f}/100",
            "search_intent": search_intent_reason,
            "information_richness": richness_reason,
            "freshness": freshness_reason,
            "audience_interest": audience_reason,
            "content_depth": depth_reason,
            "competition": competition_reason,
        },
    )


def compute_topic_purity_score(gate: RelevanceGateResult) -> tuple[int, str]:
    """topic이 얼마나 '관련 source에만' 기반하는지 — relevant/total 비율과
    strong/relevant 비율, 평균 관련성을 반영한다."""
    if gate.source_count == 0:
        return 0, "source 없음"
    relevant_ratio = gate.relevant_source_count / gate.source_count
    strong_ratio = (gate.strong_relevant_source_count / gate.relevant_source_count) if gate.relevant_source_count else 0.0
    score = round(relevant_ratio * 60 + strong_ratio * 30 + min(gate.average_relevance, 100) / 100 * 10)
    score = max(0, min(100, score))
    reason = (
        f"관련 source 비율 {relevant_ratio:.0%}, 핵심 source 비율 {strong_ratio:.0%}, "
        f"평균 관련성 {gate.average_relevance:.0f}"
    )
    return score, reason


def compute_final_score(trend_score: int, blog_score: int, editorial_bonus: int = 0) -> int:
    """Trend/Blog 점수에 선택적인 편집 방향 보정을 더한다."""
    return round(trend_score * TREND_WEIGHT + blog_score * BLOG_WEIGHT + editorial_bonus)


def compute_article_eligibility(gate: RelevanceGateResult, topic_purity: int, min_purity: int = MIN_TOPIC_PURITY) -> tuple[bool, str]:
    """Eligibility FAIL → FinalScore 계산 이전에 제외. Relevance Gate를 먼저 보고,
    통과했더라도 topic_purity가 낮으면(관련 source 비율은 낮은데 평균 점수만 높은 경우 등)
    추가로 차단한다."""
    if gate.status == "blocked":
        return False, gate.block_reason
    if topic_purity < min_purity:
        return False, "LOW_TOPIC_PURITY"
    return True, ""


# ── Topic Deduplication ───────────────────────────────────────────────────────
# "취업자 수 증가"/"청년 고용"처럼 단어가 겹치지 않는 동의어 관계는 키워드 Jaccard로
# 잡히지 않는다 — LLM에게 한 번에 후보 topic 목록을 묶어달라고 요청한다(analyze()당
# 1회 배치 호출이라 비용이 크지 않다). 실패하면 키워드+카테고리 기반 fallback으로 내려간다.

def _keyword_signature(topic: str) -> set[str]:
    return set(_tokenize(topic))


def _fallback_cluster_map(topics: list[dict]) -> dict[str, str]:
    """LLM 클러스터링 실패 시 키워드 Jaccard + 동일 category로만 대체 판단."""
    clusters: list[dict] = []  # [{"rep": str, "category": str, "sig": set[str]}]
    mapping: dict[str, str] = {}
    for t in topics:
        sig = _keyword_signature(t["topic"])
        match = None
        for c in clusters:
            if c["category"] != t["category"]:
                continue
            overlap = len(sig & c["sig"]) / max(1, len(sig | c["sig"]))
            if overlap >= CLUSTER_SIMILARITY_THRESHOLD:
                match = c
                break
        if match is None:
            clusters.append({"rep": t["topic"], "category": t["category"], "sig": sig})
            mapping[t["topic"]] = t["topic"]
        else:
            match["sig"] |= sig
            mapping[t["topic"]] = match["rep"]
    return mapping


async def _llm_cluster_map(topics: list[dict]) -> dict[str, str] | None:
    """Gemini에게 후보 topic 목록을 한 번에 넘겨 의미상 겹치는 topic을 cluster로 묶게 한다.
    실패하면 None을 반환 — 호출부가 키워드 기반 fallback으로 내려간다."""
    if len(topics) <= 1:
        return None
    from google import genai
    from google.genai import types
    import os

    api_key = os.getenv("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)

    topic_lines = "\n".join(f"- ({t['category']}) {t['topic']}" for t in topics)
    prompt = (
        "아래는 블로그 글감 후보 topic 목록이다. 의미가 겹치는(같은 사건·같은 주제를 다른 "
        "표현으로 가리키는) topic들을 하나의 cluster로 묶어라.\n"
        "예: '취업자 수 증가', '청년 고용', '청년 취업', '고용률 상승'은 모두 '고용/청년' "
        "cluster로 묶일 수 있다. 반대로 '청년 고용 증가'와 '청년 창업 지원'처럼 같은 대상을 "
        "다뤄도 검색 의도가 명확히 다르면 별개 cluster로 남겨라.\n"
        "모든 topic은 정확히 하나의 cluster에 속해야 한다 (겹치는 게 없으면 자기 자신만 있는 "
        "cluster).\n\n"
        f"topic 목록:\n{topic_lines}\n\n"
        "출력 JSON 배열만, 설명 금지:\n"
        '[{"cluster_name": "...", "topics": ["...", "..."]}]'
    )
    response_schema = types.Schema(
        type=types.Type.ARRAY,
        items=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "cluster_name": types.Schema(type=types.Type.STRING),
                "topics": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
            },
            required=["cluster_name", "topics"],
        ),
    )
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.0,
                max_output_tokens=2048,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
    except Exception as e:
        logger.warning("LLM topic clustering failed: %s", e)
        return None

    known_topics = {t["topic"] for t in topics}
    mapping: dict[str, str] = {}
    for group in parsed:
        if not isinstance(group, dict):
            continue
        cluster_name = str(group.get("cluster_name", "")).strip()
        for name in group.get("topics", []):
            name = str(name).strip()
            if name in known_topics:
                mapping[name] = cluster_name or name
    # 모델이 빠뜨린 topic은 자기 자신을 cluster로 — 묵묵히 사라지지 않게 방어.
    for t in topics:
        mapping.setdefault(t["topic"], t["topic"])
    return mapping


async def cluster_topics(results: list[dict], max_per_cluster: int = MAX_TOPICS_PER_CLUSTER) -> list[dict]:
    """final_score 내림차순으로 각 topic에 cluster/is_duplicate를 부여한다.
    같은 cluster에서 max_per_cluster를 넘는 topic은 is_duplicate=True로 표시된다
    (제거는 호출부가 blog_top10 조립 시 수행)."""
    if not results:
        return results

    ordered = sorted(results, key=lambda r: -(r.get("final_score") or 0))
    mapping = await _llm_cluster_map(ordered)
    if mapping is None:
        mapping = _fallback_cluster_map(ordered)

    cluster_seen: dict[str, int] = {}
    for r in ordered:
        cluster = mapping.get(r["topic"], r["topic"])
        r["cluster"] = cluster
        count = cluster_seen.get(cluster, 0)
        r["is_duplicate"] = count >= max_per_cluster
        cluster_seen[cluster] = count + 1
    return ordered
