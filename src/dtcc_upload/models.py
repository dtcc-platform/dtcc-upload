from __future__ import annotations

import unicodedata
import re
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dtcc_upload.paths import validate_package_path


MANIFEST_V2_SCHEMA_VERSION = "dtcc-dataset-manifest-v2"
MANIFEST_V3_SCHEMA_VERSION = "dtcc-dataset-manifest-v3"
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


class ManifestV3Model(ManifestV2Model):
    """Canonical package envelope; model bytes are opaque to the upload service.

    Preserve all context and artifact extensions. Core validates native model
    meaning; this service validates the envelope and uploaded byte integrity.
    """

    schema_version: Literal["dtcc-dataset-manifest-v3"]

    @model_validator(mode="after")
    def validate_canonical_artifacts(self) -> ManifestV3Model:
        canonical = [item for item in self.artifacts if item.role == "canonical_model"]
        if len(canonical) != 1:
            raise ValueError("Canonical package requires exactly one canonical_model artifact")
        model = canonical[0]
        if (model.format != "dtcc" or model.media_type != "application/vnd.dtcc.model+protobuf"
                or model.data_kind != "model" or getattr(model, "derived_from", None) is not None):
            raise ValueError("Invalid canonical model artifact declaration")
        model_type = getattr(model, "model_type", None)
        wire_version = getattr(model, "model_schema_version", None)
        if not isinstance(model_type, str) or not model_type.strip():
            raise ValueError("Canonical artifact requires model_type")
        if type(wire_version) is not int or wire_version <= 0:
            raise ValueError("Canonical artifact requires a positive model_schema_version")
        for artifact in self.artifacts:
            if artifact.path == "manifest.json":
                raise ValueError("Artifact path must not overwrite manifest.json")
            if artifact.size is None or artifact.sha256 is None:
                raise ValueError("Canonical package artifacts require size and sha256")
            if artifact is not model and (
                artifact.role != "derived" or getattr(artifact, "derived_from", None) != model.path
            ):
                raise ValueError("Derived artifacts must reference the canonical model")
        return self
