from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EvidenceArticle(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(pattern=r"^https?://", max_length=2000)
    publisher: str = Field(default="", max_length=200)
    published_at: datetime | None = None
    summary: str = Field(default="", max_length=2000)


class TrendCandidate(BaseModel):
    food_name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    trend_reason_ko: str = Field(min_length=1, max_length=1200)
    evidence_indexes: list[int] = Field(min_length=1)
    suitable_for_japan: bool = True


class TrendDraft(BaseModel):
    title_ko: str = Field(min_length=1, max_length=200)
    title_ja: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    description_ja: str = Field(min_length=1, max_length=3000)
    trend_reason_ja: str = Field(min_length=1, max_length=2000)
    ingredients_ja: list[str] | None = None
    steps_ja: list[str] | None = None
    cooking_time_minutes: int | None = Field(default=None, ge=1, le=1440)
    difficulty: Literal["easy", "medium", "hard"] | None = None
    source_urls: list[str] = Field(min_length=1)

    @field_validator("source_urls")
    @classmethod
    def source_urls_are_http(cls, values: list[str]) -> list[str]:
        if any(not value.startswith(("http://", "https://")) for value in values):
            raise ValueError("source_urls must contain only HTTP URLs")
        return list(dict.fromkeys(values))

    @field_validator("steps_ja")
    @classmethod
    def recipe_fields_are_consistent(cls, value, info):
        ingredients = info.data.get("ingredients_ja")
        if (ingredients is None) != (value is None):
            raise ValueError("ingredients_ja and steps_ja must both be null or arrays")
        return value


class TrendUpdate(BaseModel):
    title_ko: str | None = Field(default=None, min_length=1, max_length=200)
    title_ja: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    description_ja: str | None = Field(default=None, min_length=1, max_length=3000)
    trend_reason_ja: str | None = Field(default=None, min_length=1, max_length=2000)
    image_url: str | None = Field(default=None, max_length=2000)
    ingredients_ja: list[str] | None = None
    steps_ja: list[str] | None = None
    cooking_time_minutes: int | None = Field(default=None, ge=1, le=1440)
    difficulty: Literal["easy", "medium", "hard"] | None = None
    source_urls: list[str] | None = None
    related_recipe_id: str | None = Field(default=None, max_length=200)

    @field_validator("image_url")
    @classmethod
    def image_url_is_http(cls, value: str | None) -> str | None:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("image_url must be an HTTP URL")
        return value

    @field_validator("source_urls")
    @classmethod
    def update_source_urls_are_http(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and any(not value.startswith(("http://", "https://")) for value in values):
            raise ValueError("source_urls must contain only HTTP URLs")
        return list(dict.fromkeys(values)) if values is not None else None

    @model_validator(mode="after")
    def recipe_fields_are_consistent(self):
        fields = self.model_fields_set
        if "ingredients_ja" in fields or "steps_ja" in fields:
            if "ingredients_ja" not in fields or "steps_ja" not in fields:
                raise ValueError("ingredients_ja and steps_ja must be updated together")
            if (self.ingredients_ja is None) != (self.steps_ja is None):
                raise ValueError("ingredients_ja and steps_ja must both be null or arrays")
        return self
