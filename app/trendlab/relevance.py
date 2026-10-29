"""TrendLab Source Relevance Gate.

    [Collector] → [Topic] → [Source-Topic Matching] → **Relevance Gate** → [Article Generator]

analyzer.py의 _topic_matches()가 1차로 source_items를 topic에 묶어주지만, 그건 "topic 문구의
단어가 제목에 있는가"만 보는 느슨한 필터다. 여기서 생성 직전에 한 번 더 엄격하게 검사해서,
관련 source가 부족하면 아예 article generation을 시작하지 않는다 — "제목은 정해졌으니 어떻게든
분량을 채운다"가 아니라 "이 topic을 뒷받침하는 자료가 실제로 충분한가"를 먼저 묻는다.

점수 산정은 이 시스템 안에서 결정적(deterministic) 텍스트 중첩 기반으로 한다 — 기사 생성마다
추가 Gemini 호출을 하지 않기 위한 의도적 선택(비용/지연 대비 정확도 트레이드오프)이다. 반환
형태(relevance_score/relevance_reason/directly_supports_topic)는 LLM 기반 스코어러로 나중에
교체해도 호출부가 바뀌지 않도록 고정해뒀다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_RELEVANT_SOURCE_COUNT = 2
MIN_STRONG_RELEVANT_SOURCES = 1
MIN_AVERAGE_RELEVANCE = 65.0
RELEVANT_THRESHOLD = 50.0  # 이 미만은 절대 근거로 쓰지 않는다
STRONG_RELEVANT_THRESHOLD = 90.0  # 이 이상만 "topic을 직접 다루는 핵심 source"로 인정


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[가-힣A-Za-z0-9]+", text) if len(w) >= 2]


def score_source(topic: str, source_item: dict) -> dict:
    """source_item 하나가 topic과 얼마나 직접적으로 관련 있는지 0~100으로 평가.

    "IFA와 기술적으로 연관되어 있을 수 있습니다" 같은 추상적 근거는 인정하지 않는다 —
    실제 제목/설명에 등장하는 단어 중첩만 근거로 삼는다.

    반환: {"relevance_score": int, "relevance_reason": str, "directly_supports_topic": bool}
    """
    title = source_item.get("title", "") or ""
    desc = source_item.get("description", "") or ""
    topic_words = _tokenize(topic)
    if not topic_words:
        return {"relevance_score": 0, "relevance_reason": "topic이 비어있음", "directly_supports_topic": False}

    non_numeric = [w for w in topic_words if not w.isdigit()]
    title_hits = [w for w in topic_words if w in title]
    non_numeric_title_hits = [w for w in non_numeric if w in title]
    desc_only_hits = [w for w in non_numeric if w in desc and w not in title]

    # 제목에 숫자 아닌 핵심 단어가 하나도 없으면 이 source는 topic을 직접 다루지 않는 것으로 본다
    # (연도 같은 숫자 하나만 겹치는 기사를 근거로 인정하면 "2026" 하나로 무관한 기사가 다 들어온다).
    if not non_numeric_title_hits:
        if desc_only_hits:
            return {
                "relevance_score": 35,
                "relevance_reason": f"제목에는 핵심 단어가 없고 설명에만 '{', '.join(desc_only_hits)}' 언급됨",
                "directly_supports_topic": False,
            }
        return {
            "relevance_score": 0,
            "relevance_reason": "제목·설명 어디에도 topic의 핵심 단어가 없음",
            "directly_supports_topic": False,
        }

    if topic.strip() and topic.strip() in title:
        return {
            "relevance_score": 98,
            "relevance_reason": f"제목에 topic 문구 '{topic.strip()}'가 그대로 포함됨",
            "directly_supports_topic": True,
        }

    title_hit_ratio = len(title_hits) / len(topic_words)
    if title_hit_ratio >= 1.0:
        return {
            "relevance_score": 92,
            "relevance_reason": f"제목에 topic 단어 전부({', '.join(title_hits)}) 등장",
            "directly_supports_topic": True,
        }

    if len(non_numeric_title_hits) >= max(1, len(non_numeric) - 1) and title_hit_ratio >= 0.5:
        return {
            "relevance_score": 78,
            "relevance_reason": f"제목에 핵심 단어 대부분({', '.join(non_numeric_title_hits)}) 등장",
            "directly_supports_topic": True,
        }

    return {
        "relevance_score": 55,
        "relevance_reason": f"제목에 핵심 단어 일부({', '.join(non_numeric_title_hits)})만 등장",
        "directly_supports_topic": False,
    }


@dataclass
class RelevanceGateResult:
    status: str  # "ok" | "blocked"
    relevant_sources: list[dict]
    source_count: int
    relevant_source_count: int
    strong_relevant_source_count: int
    average_relevance: float
    block_reason: str = ""
    latest_relevant_source: str | None = None
    scored_sources: list[dict] = field(default_factory=list)

    def to_log_dict(self, topic: str) -> dict:
        return {
            "topic": topic,
            "source_count": self.source_count,
            "relevant_source_count": self.relevant_source_count,
            "strong_relevant_source_count": self.strong_relevant_source_count,
            "average_relevance": round(self.average_relevance, 1),
            "status": self.status,
            "block_reason": self.block_reason,
            "latest_relevant_source": self.latest_relevant_source,
        }


def gate_sources(
    topic: str,
    source_items: list[dict],
    min_relevant_count: int = MIN_RELEVANT_SOURCE_COUNT,
    min_strong_relevant_count: int = MIN_STRONG_RELEVANT_SOURCES,
    min_average_relevance: float = MIN_AVERAGE_RELEVANCE,
) -> RelevanceGateResult:
    """topic 근거로 쓸 수 있는 source만 추려내고, 기준 미달이면 status="blocked"로 알린다.

    관련 source가 없는 것도 정상적인 시스템 상태다 — 억지로 통과시키지 않는다.
    """
    scored = [{**item, **score_source(topic, item)} for item in source_items]
    relevant = [s for s in scored if s["relevance_score"] >= RELEVANT_THRESHOLD]
    relevant.sort(key=lambda s: s.get("published_at") or "", reverse=True)

    relevant_count = len(relevant)
    strong_count = sum(1 for s in relevant if s["relevance_score"] >= STRONG_RELEVANT_THRESHOLD)
    average = sum(s["relevance_score"] for s in relevant) / relevant_count if relevant_count else 0.0
    # relevant는 published_at 내림차순 정렬돼 있으므로 첫 항목이 가장 최신 "관련" source —
    # scoring.compute_freshness_score()가 이 값을 그대로 파싱해 신선도를 계산한다.
    latest = relevant[0].get("published_at") if relevant else None

    def _blocked(reason: str) -> RelevanceGateResult:
        return RelevanceGateResult(
            status="blocked",
            relevant_sources=relevant,
            source_count=len(source_items),
            relevant_source_count=relevant_count,
            strong_relevant_source_count=strong_count,
            average_relevance=average,
            block_reason=reason,
            latest_relevant_source=latest,
            scored_sources=scored,
        )

    if relevant_count < min_relevant_count:
        return _blocked("NO_RELEVANT_SOURCES" if relevant_count == 0 else "INSUFFICIENT_RELEVANT_SOURCES")
    if strong_count < min_strong_relevant_count:
        return _blocked("LOW_SOURCE_RELEVANCE")
    if average < min_average_relevance:
        return _blocked("LOW_AVERAGE_RELEVANCE")

    return RelevanceGateResult(
        status="ok",
        relevant_sources=relevant,
        source_count=len(source_items),
        relevant_source_count=relevant_count,
        strong_relevant_source_count=strong_count,
        average_relevance=average,
        latest_relevant_source=latest,
        scored_sources=scored,
    )


class TrendLabBlockedError(RuntimeError):
    """TrendLab article generation이 품질 게이트에 의해 차단됨 — 서버 오류가 아니라
    "이 topic은 쓸 가치가 없다"는 정상적인 시스템 판단이다. main.py에서 502가 아니라
    422 + 구조화된 block 정보로 응답해야 한다."""

    def __init__(self, topic: str, block_reason: str, detail: dict):
        self.topic = topic
        self.block_reason = block_reason
        self.detail = detail
        super().__init__(f"'{topic}' article generation 차단: {block_reason}")


class InsufficientSourcesError(TrendLabBlockedError):
    def __init__(self, topic: str, gate: RelevanceGateResult):
        self.gate = gate
        super().__init__(topic, gate.block_reason, gate.to_log_dict(topic))
