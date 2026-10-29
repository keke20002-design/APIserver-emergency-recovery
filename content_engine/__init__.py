"""ContentGenerator 레지스트리."""
from content_engine.base import ContentGenerator, GeneratedContent


class GeneratorRegistry:
    _registry: dict[str, type[ContentGenerator]] = {}

    @classmethod
    def register(cls, gen_cls: type[ContentGenerator]) -> None:
        key = f"{gen_cls.platform}:{gen_cls.language}"
        cls._registry[key] = gen_cls

    @classmethod
    def get(cls, platform: str, language: str = "ko") -> type[ContentGenerator] | None:
        return cls._registry.get(f"{platform}:{language}")

    @classmethod
    def create(cls, platform: str, language: str = "ko") -> ContentGenerator:
        gen_cls = cls.get(platform, language)
        if not gen_cls:
            raise ValueError(f"Unknown generator: {platform}/{language}")
        return gen_cls()

    @classmethod
    def keys(cls) -> list[str]:
        return list(cls._registry.keys())


# 기본 등록
from content_engine.wordpress.generator import WordPressKRGenerator, WordPressENGenerator
from content_engine.naver.generator import NaverKRGenerator

GeneratorRegistry.register(WordPressKRGenerator)
GeneratorRegistry.register(WordPressENGenerator)
GeneratorRegistry.register(NaverKRGenerator)

__all__ = ["ContentGenerator", "GeneratedContent", "GeneratorRegistry",
           "WordPressKRGenerator", "WordPressENGenerator", "NaverKRGenerator"]
