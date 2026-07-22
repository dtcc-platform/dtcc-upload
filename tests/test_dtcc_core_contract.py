from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from dtcc_upload.models import ManifestV2Model
from dtcc_upload.validation import validate_manifest


@pytest.fixture
def core_contract_directory() -> Path:
    value = os.environ.get("DTCC_CORE_CONTRACT_DIR")
    if not value:
        pytest.skip("DTCC_CORE_CONTRACT_DIR is only set by the contract workflow")

    path = Path(value)
    assert path.is_dir(), path
    return path


def test_dtcc_core_golden_package_is_accepted(client, core_contract_directory: Path):
    package_path = core_contract_directory / "golden.dtccpkg"
    assert package_path.is_file(), package_path

    with zipfile.ZipFile(package_path) as archive:
        members = {info.filename for info in archive.infolist() if not info.is_dir()}
        manifest_bytes = archive.read("manifest.json")
        manifest = validate_manifest(json.loads(manifest_bytes))

        assert isinstance(manifest, ManifestV2Model)
        artifact_paths = {artifact.path for artifact in manifest.artifacts}
        assert members == {"manifest.json", *artifact_paths}

        multipart_files = [
            ("manifest", ("manifest.json", manifest_bytes, "application/json")),
            *[
                (
                    "files",
                    (
                        artifact.path,
                        archive.read(artifact.path),
                        artifact.media_type,
                    ),
                )
                for artifact in manifest.artifacts
            ],
        ]

    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "core-golden-city"},
        files=multipart_files,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "committed"
    assert {item["path"] for item in payload["files"]} == artifact_paths
