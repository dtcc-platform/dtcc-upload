from __future__ import annotations

import math
import re
from typing import Any

from pydantic import ValidationError

from dtcc_upload.models import (
    MANIFEST_V2_SCHEMA_VERSION, MANIFEST_V3_SCHEMA_VERSION,
    ManifestModel, ManifestV2Model, ManifestV3Model,
)
from dtcc_upload.paths import validate_logical_path as _validate_logical_path
from dtcc_upload.paths import validate_package_path as _validate_package_path


DATASET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


class ManifestValidationError(ValueError):
    pass


def validate_dataset_key(value: str) -> str:
    if not isinstance(value, str) or not DATASET_KEY_RE.fullmatch(value):
        raise ValueError("Invalid dataset_key")
    return value


def validate_logical_path(value: str) -> str:
    return _validate_logical_path(value)


def validate_package_path(value: str) -> str:
    return _validate_package_path(value)


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ManifestValidationError("Manifest contains non-finite number")
    if isinstance(value, dict):
        for child in value.values():
            _reject_non_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_non_finite(child)


def validate_manifest(raw: dict[str, Any]) -> ManifestModel | ManifestV2Model:
    if not isinstance(raw, dict):
        raise ManifestValidationError("Manifest must be a JSON object")
    _reject_non_finite(raw)
    schema_version = raw.get("schema_version")
    if schema_version is not None and schema_version not in {MANIFEST_V2_SCHEMA_VERSION, MANIFEST_V3_SCHEMA_VERSION}:
        raise ManifestValidationError(
            f"Unsupported manifest schema_version: {schema_version!r}; "
            "expected Dataset Manifest v2 or v3"
        )
    if schema_version is None and ("artifacts" in raw or "identity" in raw):
        raise ManifestValidationError(
            f"Dataset Manifest v2 packages must declare schema_version={MANIFEST_V2_SCHEMA_VERSION!r}"
        )
    try:
        if schema_version == MANIFEST_V3_SCHEMA_VERSION:
            manifest = ManifestV3Model.model_validate(raw)
        elif schema_version == MANIFEST_V2_SCHEMA_VERSION:
            manifest = ManifestV2Model.model_validate(raw)
        else:
            manifest = ManifestModel.model_validate(raw)
    except ValidationError as exc:
        raise ManifestValidationError(str(exc)) from exc
    _reject_non_finite(manifest.model_dump(mode="python"))

    try:
        if isinstance(manifest, ManifestV2Model):
            seen: set[str] = set()
            for artifact in manifest.artifacts:
                artifact.path = validate_package_path(artifact.path)
                if artifact.path in seen:
                    raise ValueError("Duplicate artifact path")
                seen.add(artifact.path)
        else:
            manifest.file = validate_logical_path(manifest.file)
            if manifest.files is not None:
                manifest.files = [validate_logical_path(path) for path in manifest.files]
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc
    return manifest


def extract_manifest_file_paths(manifest: ManifestModel | ManifestV2Model) -> list[str]:
    if isinstance(manifest, ManifestV2Model):
        return [validate_package_path(artifact.path) for artifact in manifest.artifacts]
    return [validate_logical_path(manifest.file)]
