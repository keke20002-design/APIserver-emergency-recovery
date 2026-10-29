"""Publisher 레지스트리 — 플랫폼별 Publisher 등록/조회."""
from publisher.base import PostPayload, PublishResult, Publisher


class PublisherRegistry:
    _registry: dict[str, type[Publisher]] = {}

    @classmethod
    def register(cls, publisher_cls: type[Publisher]) -> None:
        cls._registry[publisher_cls.platform] = publisher_cls

    @classmethod
    def get(cls, platform: str) -> type[Publisher] | None:
        return cls._registry.get(platform)

    @classmethod
    def platforms(cls) -> list[str]:
        return list(cls._registry.keys())

    @classmethod
    def create(cls, platform: str, **kwargs) -> Publisher:
        pub_cls = cls._registry.get(platform)
        if not pub_cls:
            raise ValueError(f"Unknown publisher platform: {platform}")
        return pub_cls(**kwargs)


# 기본 등록
from publisher.wordpress.publisher import WordPressPublisher
from publisher.naver.publisher import NaverPublisher

PublisherRegistry.register(WordPressPublisher)
PublisherRegistry.register(NaverPublisher)

__all__ = ["Publisher", "PostPayload", "PublishResult", "PublisherRegistry",
           "WordPressPublisher", "NaverPublisher"]
