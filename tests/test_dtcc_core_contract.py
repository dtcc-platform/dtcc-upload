from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from dtcc_upload.models import ManifestV2Model, ManifestV3Model
from dtcc_upload.validation import validate_manifest


@pytest.fixture
def core_contract_directory() -> Path:
    value = os.environ.get("DTCC_CORE_CONTRACT_DIR")
    if not value:
        pytest.skip("DTCC_CORE_CONTRACT_DIR is only set by the contract workflow")

    path = Path(value)
    assert path.is_dir(), path
    return path


@pytest.mark.parametrize("package_name", ["golden.dtccpkg", "canonical.dtccpkg"])
def test_dtcc_core_golden_package_is_accepted(client, core_contract_directory: Path, package_name, tmp_path):
    package_path = core_contract_directory / package_name
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

    # This service promises exact transport, including the entire context and
    # opaque model artifact. Retrieve through the ordinary authenticated API.
    base = f"/v1/datasets/core-golden-city/versions/{payload['version_id']}"
    headers = {"Authorization": "Bearer browser-token"}
    downloaded_manifest = client.get(base + "/manifest", headers=headers)
    assert downloaded_manifest.content == manifest_bytes
    with zipfile.ZipFile(package_path) as archive:
        for path in artifact_paths:
            downloaded = client.get(base + "/files/" + path, headers=headers)
            assert downloaded.status_code == 200
            assert downloaded.content == archive.read(path)
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(downloaded.content)
    (tmp_path / "manifest.json").write_bytes(downloaded_manifest.content)

    if isinstance(manifest, ManifestV3Model):
        detail = client.get("/v1/datasets", headers=headers).json()["items"][0]
        assert detail["format"] == "dtcc"
        assert detail["data_kind"] == "model"
        # Optional in the storage-only CI job; exercised with Core installed in
        # the cross-repository workflow, without adding a runtime dependency.
        try:
            from dtcc_core.datasets import load_model_package
        except ImportError:
            return
        restored = load_model_package(tmp_path)
        assert restored.data.tolist() == [[1, 2], [3, 4]]
        assert str(restored.data.dtype) == "uint8"
        assert restored.dataset_context.identity.name == "canonical-raster"


def test_canonical_upload_rejects_corrupt_artifact_and_missing_integrity(client, core_contract_directory):
    with zipfile.ZipFile(core_contract_directory / "canonical.dtccpkg") as archive:
        manifest_bytes = archive.read("manifest.json")
        raw = json.loads(manifest_bytes)
        artifacts = raw["artifacts"]
        files = [("manifest", ("manifest.json", manifest_bytes, "application/json"))]
        for index, artifact in enumerate(artifacts):
            data = archive.read(artifact["path"])
            if index == 0:
                data = bytes([data[0] ^ 1]) + data[1:]
            files.append(("files", (artifact["path"], data, artifact["media_type"])))
    response = client.post("/v1/datasets", headers={"Authorization": "Bearer vasnas-token"},
                           data={"dataset_key": "corrupt-canonical"}, files=files)
    assert response.status_code == 400
    assert "sha256" in response.json()["detail"]
    del raw["artifacts"][0]["sha256"]
    with pytest.raises(ValueError, match="size and sha256"):
        validate_manifest(raw)


@pytest.mark.parametrize("archive", [False, True])
def test_core_publication_client_round_trip(client, core_contract_directory, tmp_path, archive):
    core = pytest.importorskip("dtcc_core.datasets")
    from dtcc_core.datasets.publish import DatasetUploadClient, DatasetPackageError
    model = core.load_model_package(core_contract_directory / "canonical.dtccpkg")
    uploader = DatasetUploadClient("http://testserver", "vasnas-token", session=client)
    if archive:
        package = model.export(tmp_path / "source.dtccpkg", canonical=True)
        publication = package.publish(dataset_key="native-client", uploader=uploader)
    else:
        publication = model.publish(dataset_key="native-client", canonical=True, uploader=uploader)
    base = f"/v1/datasets/native-client/versions/{publication.version_id}"
    headers = {"Authorization": "Bearer browser-token"}
    manifest = client.get(base + "/manifest", headers=headers)
    received = tmp_path / "received"
    received.mkdir()
    (received / "manifest.json").write_bytes(manifest.content)
    for artifact in manifest.json()["artifacts"]:
        target = received / artifact["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        response = client.get(base + "/files/" + artifact["path"], headers=headers)
        assert response.status_code == 200
        target.write_bytes(response.content)
    restored = core.load_model_package(received)
    assert restored.data.tolist() == model.data.tolist()
    assert restored.dataset_context == model.dataset_context

    # The publication client admits persisted canonical packages through Core,
    # before any request can create a durable version.
    target.write_bytes(b"corrupt")
    with pytest.raises(DatasetPackageError, match="Invalid canonical package"):
        uploader.upload_package(dataset_key="invalid-client", manifest_path=received / "manifest.json",
                                files=[target])
