from __future__ import annotations

import unicodedata
import re
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dtcc_upload.paths import validate_package_path


MANIFEST_V2_SCHEMA_VERSION = "dtcc-dataset-manifest-v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


class ManifestV2Identity(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=256)

    @field_validator("name", "title")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


class ManifestV2Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")


class ManifestV2Provenance(BaseModel):
    model_config = ConfigDict(extra="allow")


class ManifestV2Presentation(BaseModel):
    model_config = ConfigDict(extra="allow")


class ManifestV2Request(BaseModel):
    model_config = ConfigDict(extra="allow")

    dataset_name: str | None = Field(default=None, max_length=256)
    parameters: dict[str, Any] | None = None
    bounds: list[float] | None = None

    @field_validator("dataset_name")
    @classmethod
    def strip_dataset_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


class ManifestV2Artifact(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1, max_length=1024)
    role: str = Field(min_length=1, max_length=64)
    format: str = Field(min_length=1, max_length=64)
    media_type: str = Field(min_length=1, max_length=128)
    data_kind: str = Field(min_length=1, max_length=64)
    crs: str | None = None
    bounds: list[float] | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return validate_package_path(value)

    @field_validator("role")
    @classmethod
    def strip_role(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("format", "media_type", "data_kind")
    @classmethod
    def lowercase_known_fields(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not SHA256_RE.fullmatch(value):
            raise ValueError("must be a 64-character lowercase SHA-256 hex digest")
        return value


class ManifestV2Model(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal["dtcc-dataset-manifest-v2"]
    identity: ManifestV2Identity
    metadata: ManifestV2Metadata = Field(default_factory=ManifestV2Metadata)
    provenance: ManifestV2Provenance = Field(default_factory=ManifestV2Provenance)
    presentation: ManifestV2Presentation = Field(default_factory=ManifestV2Presentation)
    request: ManifestV2Request = Field(default_factory=ManifestV2Request)
    artifacts: list[ManifestV2Artifact] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_artifact_paths(self) -> ManifestV2Model:
        seen: set[str] = set()
        for artifact in self.artifacts:
            if artifact.path in seen:
                raise ValueError(f"duplicate artifact path: {artifact.path}")
            seen.add(artifact.path)
        return self
