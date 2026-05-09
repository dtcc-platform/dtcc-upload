from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from pydantic import ValidationError

from dtcc_upload.models import ManifestModel


DATASET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}


class ManifestValidationError(ValueError):
    pass


def validate_dataset_key(value: str) -> str:
    if not isinstance(value, str) or not DATASET_KEY_RE.fullmatch(value):
        raise ValueError("Invalid dataset_key")
    return value


def validate_logical_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid file path")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise ValueError("Invalid file path")
    # v1 intentionally rejects dotfiles because file names become URL-visible artifacts.
    if normalized.startswith("/") or normalized.startswith("."):
        raise ValueError("Invalid file path")
    if "/" in normalized or "\\" in normalized or "\x00" in normalized:
        raise ValueError("Invalid file path")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("Invalid file path")
    if ".." in normalized:
        raise ValueError("Invalid file path")
    if len(normalized.encode("utf-8")) > 255:
        raise ValueError("Invalid file path")

    basename = normalized.rsplit(".", 1)[0].lower()
    if basename in WINDOWS_RESERVED_BASENAMES:
        raise ValueError("Invalid file path")
    return normalized


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ManifestValidationError("Manifest contains non-finite number")
    if isinstance(value, dict):
        for child in value.values():
            _reject_non_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_non_finite(child)


def validate_manifest(raw: dict[str, Any]) -> ManifestModel:
    if not isinstance(raw, dict):
        raise ManifestValidationError("Manifest must be a JSON object")
    _reject_non_finite(raw)
    try:
        manifest = ManifestModel.model_validate(raw)
    except ValidationError as exc:
        raise ManifestValidationError(str(exc)) from exc
    _reject_non_finite(manifest.model_dump(mode="python"))

    try:
        manifest.file = validate_logical_path(manifest.file)
        if manifest.files is not None:
            manifest.files = [validate_logical_path(path) for path in manifest.files]
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc
    return manifest


def extract_manifest_file_paths(manifest: ManifestModel) -> list[str]:
    return [validate_logical_path(manifest.file)]
