from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00"
    b"\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
GEOJSON_BYTES = b'{"type":"FeatureCollection","features":[]}'


def _version_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0])


def _file_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _v2_artifact(
    path: str,
    *,
    role: str = "primary",
    format: str = "png",
    media_type: str = "image/png",
    data_kind: str = "raster",
    bounds: list[float] | None = None,
    size: int | None = None,
    sha256: str | None = None,
) -> dict[str, object]:
    artifact: dict[str, object] = {
        "path": path,
        "role": role,
        "format": format,
        "media_type": media_type,
        "data_kind": data_kind,
    }
    if bounds is not None:
        artifact["bounds"] = bounds
    if size is not None:
        artifact["size"] = size
    if sha256 is not None:
        artifact["sha256"] = sha256
    return artifact


def _v2_manifest_bytes(*artifacts: dict[str, object]) -> bytes:
    return json.dumps(
        {
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
        },
        sort_keys=True,
    ).encode("utf-8")


def _upload_v2(client, *, dataset_key: str, manifest_bytes: bytes, upload_files: list[tuple[str, bytes, str]]):
    return client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": dataset_key},
        files=[
            ("manifest", ("manifest.json", manifest_bytes, "application/json")),
            *[
                ("files", (filename, payload, media_type))
                for filename, payload, media_type in upload_files
            ],
        ],
    )


def test_upload_requires_upload_scope(client, manifest_bytes_factory):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer browser-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )
    assert response.status_code == 403


def test_upload_commits_manifest_and_file(client, manifest_bytes_factory):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["dataset_key"] == "smoke-slice"
    assert data["version_number"] == 1
    assert data["status"] == "committed"
    assert data["files"][0]["path"] == "smoke_slice.geojson"


def test_upload_same_content_without_idempotency_returns_existing(client, manifest_bytes_factory):
    files = {
        "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
        "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
    }
    first = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files=files,
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files=files,
    )
    assert second.status_code == 200
    assert second.json()["version_id"] == first.json()["version_id"]


def test_upload_rejects_extra_file_in_single_file_v1(client, manifest_bytes_factory):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files=[
            ("manifest", ("manifest.json", manifest_bytes_factory(), "application/json")),
            ("files", ("smoke_slice.geojson", b"{}", "application/geo+json")),
            ("files", ("extra.geojson", b"{}", "application/geo+json")),
        ],
    )
    assert response.status_code == 400


def test_upload_rejects_chunked_transfer(client, manifest_bytes_factory):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token", "Transfer-Encoding": "chunked"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b"{}", "application/geo+json"),
        },
    )
    assert response.status_code == 411


def test_upload_rejects_extra_form_parts_over_limit(client, manifest_bytes_factory):
    client.app.state.settings = replace(
        client.app.state.settings,
        max_total_parts=4,
        max_non_file_fields=2,
    )

    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice", "extra_one": "1", "extra_two": "2"},
        files=[
            ("manifest", ("manifest.json", manifest_bytes_factory(), "application/json")),
            ("files", ("smoke_slice.geojson", b"{}", "application/geo+json")),
        ],
    )

    assert response.status_code == 413


def test_upload_cleans_pending_row_after_post_pending_failure(client, manifest_bytes_factory, db_path, storage_root, monkeypatch):
    def fail_finalize(*args, **kwargs):
        raise RuntimeError("finalize failed")

    monkeypatch.setattr("dtcc_upload.app.Storage.finalize", fail_finalize)

    with pytest.raises(RuntimeError, match="finalize failed"):
        client.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer vasnas-token"},
            data={"dataset_key": "smoke-slice"},
            files={
                "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
                "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
            },
        )

    assert _version_count(db_path) == 0
    assert _file_count(db_path) == 0
    assert not any((storage_root / "_incoming").iterdir())


def test_upload_quota_failure_cleans_pending_and_final_dir(client, manifest_bytes_factory, db_path, storage_root):
    client.app.state.settings = replace(client.app.state.settings, max_stored_bytes_per_principal=1)

    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )

    assert response.status_code == 413
    assert _version_count(db_path) == 0
    assert _file_count(db_path) == 0
    assert not (storage_root / "datasets" / "smoke-slice").exists()


def test_upload_v2_single_png_primary_artifact(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact(
            "artifacts/smoke_slice.png",
            bounds=[1, 2, 3, 4],
            size=len(PNG_BYTES),
            sha256=_sha256(PNG_BYTES),
        )
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[("smoke_slice.png", PNG_BYTES, "image/png")],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["dataset_key"] == "smoke-slice-v2"
    assert data["files"][0]["path"] == "artifacts/smoke_slice.png"
    assert data["files"][0]["original_filename"] == "smoke_slice.png"
    assert data["files"][0]["size"] == len(PNG_BYTES)
    assert data["files"][0]["sha256"] == _sha256(PNG_BYTES)
    assert data["files"][0]["media_type"] == "image/png"


def test_upload_v2_multi_artifact_catalog_summary_manifest_and_nested_download(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/smoke_slice.png", bounds=[0, 0, 10, 20]),
        _v2_artifact(
            "artifacts/smoke_slice.geojson",
            role="auxiliary",
            format="geojson",
            media_type="application/geo+json",
            data_kind="vector",
        ),
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("smoke_slice.geojson", GEOJSON_BYTES, "application/geo+json"),
            ("smoke_slice.png", PNG_BYTES, "image/png"),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    version = client.get(
        f"/v1/datasets/smoke-slice-v2/versions/{data['version_id']}",
        headers={"Authorization": "Bearer browser-token"},
    )
    manifest = client.get(
        f"/v1/datasets/smoke-slice-v2/versions/{data['version_id']}/manifest",
        headers={"Authorization": "Bearer browser-token"},
    )
    png = client.get(
        f"/v1/datasets/smoke-slice-v2/versions/{data['version_id']}/files/artifacts/smoke_slice.png",
        headers={"Authorization": "Bearer browser-token"},
    )

    assert version.status_code == 200
    version_payload = version.json()
    paths = {row["path"] for row in version_payload["files"]}
    assert paths == {"artifacts/smoke_slice.png", "artifacts/smoke_slice.geojson"}
    assert version_payload["version"]["file_count"] == 2
    assert version_payload["version"]["total_bytes"] == len(PNG_BYTES) + len(GEOJSON_BYTES)
    assert version_payload["version"]["format"] == "png"
    assert version_payload["version"]["media_type"] == "image/png"
    assert version_payload["version"]["data_kind"] == "raster"
    assert version_payload["version"]["product"] == "slice"
    assert version_payload["version"]["title"] == "Smoke"
    assert json.loads(version_payload["version"]["bounds_json"]) == [0, 0, 10, 20]
    assert manifest.status_code == 200
    assert manifest.content == manifest_bytes
    assert png.status_code == 200
    assert png.content == PNG_BYTES


def test_upload_v2_rejects_missing_artifact_upload(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/smoke_slice.png"),
        _v2_artifact(
            "artifacts/smoke_slice.geojson",
            role="auxiliary",
            format="geojson",
            media_type="application/geo+json",
            data_kind="vector",
        ),
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[("smoke_slice.png", PNG_BYTES, "image/png")],
    )

    assert response.status_code == 400
    assert "Missing uploaded files" in response.json()["detail"]


def test_upload_v2_rejects_extra_artifact_upload(client):
    manifest_bytes = _v2_manifest_bytes(_v2_artifact("artifacts/smoke_slice.png"))

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("smoke_slice.png", PNG_BYTES, "image/png"),
            ("extra.geojson", GEOJSON_BYTES, "application/geo+json"),
        ],
    )

    assert response.status_code == 400
    assert "Unexpected uploaded files" in response.json()["detail"]


def test_upload_v2_rejects_duplicate_upload_filenames(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/a.png"),
        _v2_artifact("artifacts/b.png", role="auxiliary"),
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("a.png", PNG_BYTES, "image/png"),
            ("a.png", PNG_BYTES, "image/png"),
        ],
    )

    assert response.status_code == 400
    assert "Duplicate uploaded filename" in response.json()["detail"]


def test_upload_v2_rejects_ambiguous_basename_matching(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/slice.png"),
        _v2_artifact("preview/slice.png", role="auxiliary"),
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("slice.png", PNG_BYTES, "image/png"),
            ("preview/slice.png", PNG_BYTES, "image/png"),
        ],
    )

    assert response.status_code == 400
    assert "Ambiguous uploaded filename" in response.json()["detail"]


def test_upload_v2_accepts_exact_paths_when_basenames_are_ambiguous(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/slice.png"),
        _v2_artifact("preview/slice.png", role="auxiliary"),
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("preview/slice.png", PNG_BYTES, "image/png"),
            ("artifacts/slice.png", PNG_BYTES, "image/png"),
        ],
    )

    assert response.status_code == 200
    assert {row["path"] for row in response.json()["files"]} == {
        "artifacts/slice.png",
        "preview/slice.png",
    }


def test_upload_v2_rejects_declared_media_type_mismatch(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact(
            "artifacts/smoke_slice.geojson",
            format="geojson",
            media_type="application/geo+json",
            data_kind="vector",
        )
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[("smoke_slice.geojson", b"not json", "application/geo+json")],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Declared media type does not match file content"


def test_upload_v2_rejects_artifact_sha256_mismatch(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/smoke_slice.png", sha256="a" * 64)
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[("smoke_slice.png", PNG_BYTES, "image/png")],
    )

    assert response.status_code == 400
    assert "Artifact sha256 does not match manifest" in response.json()["detail"]


def test_upload_v2_rejects_artifact_size_mismatch(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/smoke_slice.png", size=len(PNG_BYTES) + 1)
    )

    response = _upload_v2(
        client,
        dataset_key="smoke-slice-v2",
        manifest_bytes=manifest_bytes,
        upload_files=[("smoke_slice.png", PNG_BYTES, "image/png")],
    )

    assert response.status_code == 400
    assert "Artifact size does not match manifest" in response.json()["detail"]


def test_upload_v2_file_set_hash_is_independent_of_multipart_order(client):
    manifest_bytes = _v2_manifest_bytes(
        _v2_artifact("artifacts/smoke_slice.png"),
        _v2_artifact(
            "artifacts/smoke_slice.geojson",
            role="auxiliary",
            format="geojson",
            media_type="application/geo+json",
            data_kind="vector",
        ),
    )

    first = _upload_v2(
        client,
        dataset_key="smoke-order-a",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("smoke_slice.png", PNG_BYTES, "image/png"),
            ("smoke_slice.geojson", GEOJSON_BYTES, "application/geo+json"),
        ],
    )
    second = _upload_v2(
        client,
        dataset_key="smoke-order-b",
        manifest_bytes=manifest_bytes,
        upload_files=[
            ("smoke_slice.geojson", GEOJSON_BYTES, "application/geo+json"),
            ("smoke_slice.png", PNG_BYTES, "image/png"),
        ],
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["file_set_sha256"] == second.json()["file_set_sha256"]
