from __future__ import annotations

import math
import unicodedata

import pytest

from dtcc_upload.models import ManifestV2Model
from dtcc_upload.validation import (
    ManifestValidationError,
    extract_manifest_file_paths,
    validate_dataset_key,
    validate_logical_path,
    validate_manifest,
    validate_package_path,
)


def test_dataset_key_accepts_lowercase_slug():
    assert validate_dataset_key("smoke-table-slice") == "smoke-table-slice"


@pytest.mark.parametrize(
    "value",
    ["Smoke", "a", "ab", "-bad", "bad-", "bad_key", "bad/path", "bad.key"],
)
def test_dataset_key_rejects_invalid_values(value: str):
    with pytest.raises(ValueError):
        validate_dataset_key(value)


def test_dataset_key_rejects_non_ascii():
    with pytest.raises(ValueError):
        validate_dataset_key("smoke-é")


def test_validate_logical_path_accepts_simple_filename():
    assert validate_logical_path("smoke_slice.geojson") == "smoke_slice.geojson"


@pytest.mark.parametrize(
    "value",
    [
        "../x",
        "/tmp/x",
        "a/b",
        "a\\b",
        "",
        ".",
        "..",
        ".env",
        "CON",
        "nul.txt",
        "has\x00nul",
        "has\x1fcontrol",
        "middle..dots.geojson",
    ],
)
def test_validate_logical_path_rejects_unsafe_names(value: str):
    with pytest.raises(ValueError):
        validate_logical_path(value)


def test_validate_logical_path_remains_strict_for_v1_nested_names():
    with pytest.raises(ValueError):
        validate_logical_path("artifacts/smoke_slice.png")


def test_validate_logical_path_rejects_names_longer_than_255_utf8_bytes():
    value = ("a" * 252) + "é.geojson"
    assert len(value.encode("utf-8")) > 255
    with pytest.raises(ValueError):
        validate_logical_path(value)


def test_validate_logical_path_normalizes_to_nfc():
    decomposed = "cafe\u0301.geojson"
    assert validate_logical_path(decomposed) == unicodedata.normalize("NFC", decomposed)


def test_validate_manifest_accepts_dtcc_file_manifest():
    manifest = {
        "name": "smoke",
        "file": "smoke_slice.geojson",
        "format": "GeoJSON",
        "media_type": "Application/Geo+JSON",
        "data_kind": "Vector",
    }
    parsed = validate_manifest(manifest)
    assert parsed.name == "smoke"
    assert parsed.file == "smoke_slice.geojson"
    assert parsed.format == "geojson"
    assert parsed.media_type == "application/geo+json"
    assert parsed.data_kind == "vector"


def test_validate_manifest_allows_extra_fields():
    parsed = validate_manifest(
        {
            "name": "smoke",
            "file": "smoke_slice.geojson",
            "format": "geojson",
            "media_type": "application/geo+json",
            "data_kind": "vector",
            "extra_value": "kept",
        }
    )
    assert parsed.extra_value == "kept"


def test_manifest_name_is_metadata_not_dataset_key():
    parsed = validate_manifest(
        {
            "name": "Human Readable Smoke Name",
            "file": "smoke_slice.geojson",
            "format": "geojson",
            "media_type": "application/geo+json",
            "data_kind": "vector",
        }
    )
    assert parsed.name == "Human Readable Smoke Name"


def test_manifest_files_must_match_file_when_present():
    manifest = {
        "name": "smoke",
        "file": "smoke_slice.geojson",
        "files": ["other.geojson"],
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
    }
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


def test_manifest_files_and_file_are_compared_after_nfc_normalization():
    parsed = validate_manifest(
        {
            "name": "smoke",
            "file": "cafe\u0301.geojson",
            "files": ["café.geojson"],
            "format": "geojson",
            "media_type": "application/geo+json",
            "data_kind": "vector",
        }
    )
    assert parsed.file == "café.geojson"
    assert parsed.files == ["café.geojson"]


@pytest.mark.parametrize("raw", [None, [], "manifest"])
def test_validate_manifest_rejects_non_dict_raw_objects(raw):
    with pytest.raises(ManifestValidationError):
        validate_manifest(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_validate_manifest_rejects_non_finite_floats_anywhere(value: float):
    manifest = {
        "name": "smoke",
        "file": "smoke_slice.geojson",
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
        "parameters": {"nested": [value]},
    }
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


def test_validate_manifest_wraps_pydantic_validation_errors():
    manifest = {
        "name": "smoke",
        "file": "smoke_slice.geojson",
        "format": "geojson",
        "media_type": "application/geo+json",
    }
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


def test_validate_manifest_rejects_invalid_file_path():
    manifest = {
        "name": "smoke",
        "file": "../smoke_slice.geojson",
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
    }
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


def test_validate_manifest_v1_rejects_nested_file_path():
    manifest = {
        "name": "smoke",
        "file": "artifacts/smoke_slice.geojson",
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
    }
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_manifest_rejects_non_finite_bounds_after_coercion(value: str):
    manifest = {
        "name": "smoke",
        "file": "smoke_slice.geojson",
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
        "bounds": [0, value, 1, 1],
    }
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field", ["format", "media_type", "data_kind"])
def test_manifest_rejects_whitespace_only_known_fields(field: str):
    manifest = {
        "name": "smoke",
        "file": "smoke_slice.geojson",
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
    }
    manifest[field] = " "
    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest)


def test_extract_manifest_file_paths_normalizes_single_file():
    manifest = validate_manifest(
        {
            "name": "smoke",
            "file": "cafe\u0301.geojson",
            "format": "geojson",
            "media_type": "application/geo+json",
            "data_kind": "vector",
        }
    )
    assert extract_manifest_file_paths(manifest) == ["café.geojson"]


def _v2_manifest(*artifacts):
    return {
        "schema_version": "dtcc-dataset-manifest-v2",
        "identity": {"name": "smoke", "title": "Smoke"},
        "metadata": {},
        "provenance": {},
        "presentation": {},
        "request": {
            "dataset_name": "smoke",
            "parameters": {"product": "slice"},
            "bounds": [0, 0, 10, 20],
        },
        "artifacts": list(artifacts),
    }


def _v2_artifact(path: str, *, role: str = "primary", format: str = "png", media_type: str = "image/png"):
    return {
        "path": path,
        "role": role,
        "format": format,
        "media_type": media_type,
        "data_kind": "Raster" if format == "png" else "Vector",
    }


def test_validate_package_path_accepts_nested_artifact_paths():
    assert validate_package_path("artifacts/smoke_slice.png") == "artifacts/smoke_slice.png"
    assert validate_package_path("artifacts/smoke_slice.geojson") == "artifacts/smoke_slice.geojson"


@pytest.mark.parametrize(
    "value",
    [
        "../x.png",
        "artifacts/../x.png",
        "/artifacts/x.png",
        "artifacts/.hidden",
        "artifacts\\x.png",
        ".artifacts/x.png",
        "artifacts//x.png",
        "artifacts/./x.png",
        "C:/artifacts/x.png",
        "artifacts/has\x00nul.png",
        "artifacts/has\x1fcontrol.png",
    ],
)
def test_validate_package_path_rejects_unsafe_artifact_paths(value: str):
    with pytest.raises(ValueError):
        validate_package_path(value)


def test_validate_manifest_accepts_v2_single_artifact():
    parsed = validate_manifest(_v2_manifest(_v2_artifact("artifacts/smoke_slice.png")))

    assert isinstance(parsed, ManifestV2Model)
    assert parsed.schema_version == "dtcc-dataset-manifest-v2"
    assert parsed.artifacts[0].path == "artifacts/smoke_slice.png"
    assert parsed.artifacts[0].format == "png"
    assert parsed.artifacts[0].media_type == "image/png"
    assert parsed.artifacts[0].data_kind == "raster"
    assert extract_manifest_file_paths(parsed) == ["artifacts/smoke_slice.png"]


def test_validate_manifest_accepts_v2_multiple_artifacts():
    parsed = validate_manifest(
        _v2_manifest(
            _v2_artifact("artifacts/smoke_slice.png"),
            _v2_artifact(
                "artifacts/smoke_slice.geojson",
                role="auxiliary",
                format="geojson",
                media_type="application/geo+json",
            ),
        )
    )

    assert isinstance(parsed, ManifestV2Model)
    assert extract_manifest_file_paths(parsed) == [
        "artifacts/smoke_slice.png",
        "artifacts/smoke_slice.geojson",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "../x.png",
        "artifacts/../x.png",
        "/artifacts/x.png",
        "artifacts/.hidden",
        "artifacts\\x.png",
    ],
)
def test_validate_manifest_rejects_v2_unsafe_artifact_paths(path: str):
    with pytest.raises(ManifestValidationError):
        validate_manifest(_v2_manifest(_v2_artifact(path)))


def test_validate_manifest_rejects_v2_invalid_sha256():
    artifact = _v2_artifact("artifacts/smoke_slice.png")
    artifact["sha256"] = "A" * 64

    with pytest.raises(ManifestValidationError):
        validate_manifest(_v2_manifest(artifact))


def test_validate_manifest_rejects_v2_shape_without_schema_version_even_if_v1_fields_exist():
    manifest = _v2_manifest(_v2_artifact("artifacts/smoke_slice.png"))
    del manifest["schema_version"]
    manifest.update(
        {
            "name": "smoke",
            "file": "smoke_slice.geojson",
            "format": "geojson",
            "media_type": "application/geo+json",
            "data_kind": "vector",
        }
    )

    with pytest.raises(ManifestValidationError, match="must declare schema_version"):
        validate_manifest(manifest)


def test_validate_manifest_rejects_wrong_schema_version_even_if_v1_fields_exist():
    manifest = _v2_manifest(_v2_artifact("artifacts/smoke_slice.png"))
    manifest["schema_version"] = "dtcc-dataset-manifest-v3"
    manifest.update(
        {
            "name": "smoke",
            "file": "smoke_slice.geojson",
            "format": "geojson",
            "media_type": "application/geo+json",
            "data_kind": "vector",
        }
    )

    with pytest.raises(ManifestValidationError, match="Unsupported manifest schema_version"):
        validate_manifest(manifest)
