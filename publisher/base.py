"""Publisher 추상 기반 클래스."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PostPayload:
    keyword: str
    topic: str
    title: str
    content: str
    tags: list[str]
    meta_description: str
    summary: list[str] = field(default_factory=list)
    language: str = "ko"
    thumbnail_url: str | None = None
    featured_image_id: int | None = None
    img_bytes: bytes | None = None
    img_mime: str | None = None
    traffic_score: int | None = None
    competition: int | None = None
    trend_level: str | None = None
    score_reason: list[str] = field(default_factory=list)
    ctr_score: int | None = None
    seo_score: int | None = None
    ai_risk: str | None = None
    dopamine_score: int | None = None


@dataclass
class PublishResult:
    platform: str
    status: str
    post_id: str | None = None
    url: str | None = None
    error: str | None = None


class Publisher(ABC):
    platform: str = ""

    @abstractmethod
    async def publish(self, payload: PostPayload) -> PublishResult:
        ...
