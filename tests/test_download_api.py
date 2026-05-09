from __future__ import annotations

from fastapi.testclient import TestClient


def _upload(client, manifest_bytes_factory, *, dataset_key: str = "smoke-slice"):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": dataset_key},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )
    assert response.status_code == 200
    return response.json()


def test_download_manifest_with_etag_nosniff_and_inline(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/manifest",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert response.status_code == 200
    assert response.headers["etag"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "inline" in response.headers["content-disposition"]


def test_download_manifest_not_modified(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    first = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/manifest",
        headers={"Authorization": "Bearer browser-token"},
    )
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/manifest",
        headers={"Authorization": "Bearer browser-token", "If-None-Match": first.headers["etag"]},
    )
    assert response.status_code == 304


def test_download_file_range(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/files/smoke_slice.geojson",
        headers={"Authorization": "Bearer browser-token", "Range": "bytes=0-3"},
    )
    assert response.status_code == 206
    assert response.content == b'{"ty'
    assert response.headers["accept-ranges"] == "bytes"


def test_download_file_suffix_range(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/files/smoke_slice.geojson",
        headers={"Authorization": "Bearer browser-token", "Range": "bytes=-4"},
    )
    assert response.status_code == 206
    assert response.content == b'on"}'


def test_download_file_invalid_range_has_content_range(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/files/smoke_slice.geojson",
        headers={"Authorization": "Bearer browser-token", "Range": "bytes=99-100"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */28"


def test_download_file_range_end_past_size_is_clamped(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/files/smoke_slice.geojson",
        headers={"Authorization": "Bearer browser-token", "Range": "bytes=0-999"},
    )
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 0-27/28"
    assert response.content == b'{"type":"FeatureCollection"}'


def test_download_file_head_has_no_body(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.head(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/files/smoke_slice.geojson",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert response.status_code == 200
    assert response.content == b""


def test_download_file_rejects_path_escape(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/files/%2E%2E%2Fmanifest.json",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert response.status_code == 404


def test_get_dataset_and_version_metadata(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    dataset = client.get(
        "/v1/datasets/smoke-slice",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert dataset.status_code == 200
    assert dataset.json()["latest"]["version_id"] == data["version_id"]

    versions = client.get(
        "/v1/datasets/smoke-slice/versions",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert versions.status_code == 200
    assert versions.json()["items"][0]["version_id"] == data["version_id"]

    version = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert version.status_code == 200
    assert version.json()["files"][0]["path"] == "smoke_slice.geojson"


def test_upload_only_token_cannot_read_committed_dataset_or_manifest(
    monkeypatch,
    storage_root,
    db_path,
    manifest_bytes_factory,
):
    monkeypatch.setenv("DTCC_UPLOAD_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DTCC_UPLOAD_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DTCC_UPLOAD_TOKENS_JSON",
        "["
        '{"token_id":"vasnas-test","principal_id":"vasnas","token":"vasnas-token","scopes":["upload","browse"]},'
        '{"token_id":"upload-only","principal_id":"vasnas","token":"upload-only-token","scopes":["upload"]}'
        "]",
    )

    from dtcc_upload.app import create_app

    with TestClient(create_app(), raise_server_exceptions=True) as test_client:
        data = _upload(test_client, manifest_bytes_factory)
        dataset = test_client.get(
            "/v1/datasets/smoke-slice",
            headers={"Authorization": "Bearer upload-only-token"},
        )
        versions = test_client.get(
            "/v1/datasets/smoke-slice/versions",
            headers={"Authorization": "Bearer upload-only-token"},
        )
        manifest = test_client.get(
            f"/v1/datasets/smoke-slice/versions/{data['version_id']}/manifest",
            headers={"Authorization": "Bearer upload-only-token"},
        )

    assert dataset.status_code == 403
    assert versions.status_code == 403
    assert manifest.status_code == 403


def test_dataset_listing_filters_and_cursor(client, manifest_bytes_factory):
    _upload(client, manifest_bytes_factory)
    _upload(client, manifest_bytes_factory, dataset_key="smoke-slice-2")
    first_page = client.get(
        "/v1/datasets?format=geojson&data_kind=vector&limit=1",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 1
    assert first_page.json()["next_cursor"] is not None
    second_page = client.get(
        f"/v1/datasets?format=geojson&data_kind=vector&limit=1&cursor={first_page.json()['next_cursor']}",
        headers={"Authorization": "Bearer browser-token"},
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["items"]) == 1
