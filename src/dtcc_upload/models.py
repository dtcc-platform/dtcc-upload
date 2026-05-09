from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=256)
    file: str = Field(min_length=1, max_length=255)
    files: list[str] | None = None
    format: str = Field(min_length=1, max_length=64)
    media_type: str = Field(min_length=1, max_length=128)
    data_kind: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=256)
    product: str | None = Field(default=None, max_length=128)
    bounds: list[float] | None = None
    parameters: dict[str, Any] | None = None
    fields: list[str] | None = None
    visualization: dict[str, Any] | None = None

    @field_validator("format", "media_type", "data_kind")
    @classmethod
    def lowercase_known_fields(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_files_consistency(self) -> ManifestModel:
        self.file = unicodedata.normalize("NFC", self.file)
        if self.files is not None:
            self.files = [unicodedata.normalize("NFC", item) for item in self.files]
            if self.files != [self.file]:
                raise ValueError("manifest.files must match [manifest.file] in v1")
        return self
