"""ContentGenerator 추상 기반 클래스."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GeneratedContent:
    keyword: str
    title: str
    content: str
    meta_description: str
    tags: list[str]
    summary: list[str] = field(default_factory=list)
    language: str = "ko"
    platform: str = "wordpress"
    category: str = ""
    # Traffic Score (Gemini raw — heuristic correction applied in pipeline)
    gemini_traffic_score: int | None = None
    competition: int | None = None
    trend_level: str | None = None       # HOT | WARM | NORMAL
    score_reason: list[str] = field(default_factory=list)


class ContentGenerator(ABC):
    platform: str = ""
    language: str = "ko"

    @abstractmethod
    async def generate(self, keyword: str, topic: str, intent: str = "") -> GeneratedContent:
        ...
