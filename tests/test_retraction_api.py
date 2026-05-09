from __future__ import annotations


def _upload(client, manifest_bytes_factory):
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
    return response.json()


def test_owner_can_retract_version(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.post(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/retract",
        headers={"Authorization": "Bearer vasnas-token"},
        json={"reason": "bad upload"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "retracted"


def test_retracting_retracted_version_preserves_audit_metadata(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    first = client.post(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/retract",
        headers={"Authorization": "Bearer vasnas-token"},
        json={"reason": "first reason"},
    )
    assert first.status_code == 200
    first_version = client.app.state.catalog.get_version("smoke-slice", data["version_id"])

    second = client.post(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/retract",
        headers={"Authorization": "Bearer vasnas-token"},
        json={"reason": "second reason"},
    )
    assert second.status_code == 200
    second_version = client.app.state.catalog.get_version("smoke-slice", data["version_id"])

    assert second_version["retraction_reason"] == "first reason"
    assert second_version["retracted_by_principal_id"] == first_version["retracted_by_principal_id"]
    assert second_version["retracted_at"] == first_version["retracted_at"]


def test_browse_token_cannot_retract(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    response = client.post(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/retract",
        headers={"Authorization": "Bearer browser-token"},
        json={"reason": "bad upload"},
    )
    assert response.status_code == 403


def test_owner_can_download_retracted_manifest_for_audit(client, manifest_bytes_factory):
    data = _upload(client, manifest_bytes_factory)
    retract = client.post(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/retract",
        headers={"Authorization": "Bearer vasnas-token"},
    )
    assert retract.status_code == 200
    response = client.get(
        f"/v1/datasets/smoke-slice/versions/{data['version_id']}/manifest",
        headers={"Authorization": "Bearer vasnas-token"},
    )
    assert response.status_code == 200
