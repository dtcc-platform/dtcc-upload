from __future__ import annotations

import sqlite3
from dataclasses import replace
from io import BytesIO

import pytest


def test_upload_rejects_html_disguised_as_geojson(client, manifest_bytes_factory):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b"<html><script>alert(1)</script>", "application/geo+json"),
        },
    )

    assert response.status_code == 400
    assert "media type" in response.json()["detail"].lower()


def test_upload_rejects_svg_disguised_as_geojson(client, manifest_bytes_factory):
    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "application/geo+json"),
        },
    )

    assert response.status_code == 400
    assert "media type" in response.json()["detail"].lower()


def test_upload_stores_sniffed_media_type(client, manifest_bytes_factory):
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
    assert response.json()["files"][0]["sniffed_media_type"] == "application/geo+json"


def test_upload_rejects_manifest_over_limit_without_full_read(client, manifest_bytes_factory):
    client.app.state.settings = replace(client.app.state.settings, max_manifest_bytes=16)

    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b"{}", "application/geo+json"),
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Manifest too large"


def test_upload_rejects_extra_form_fields_by_non_file_count(client, manifest_bytes_factory):
    client.app.state.settings = replace(
        client.app.state.settings,
        max_total_parts=99,
        max_non_file_fields=1,
    )

    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice", "extra": "1"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b"{}", "application/geo+json"),
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Too many form fields"


def test_limited_manifest_reader_never_uses_unbounded_read():
    from dtcc_upload.app import read_upload_bytes_limited

    class NoUnboundedRead(BytesIO):
        def read(self, size=-1):
            if size == -1:
                raise AssertionError("unbounded read")
            return super().read(size)

    with pytest.raises(Exception, match="Manifest too large"):
        read_upload_bytes_limited(NoUnboundedRead(b"x" * 17), max_bytes=16, overflow_detail="Manifest too large")


def test_abort_event_failure_does_not_mask_upload_error_or_skip_cleanup(
    client,
    manifest_bytes_factory,
    monkeypatch,
    storage_root,
):
    original_record_event = client.app.state.catalog.record_event

    def flaky_record_event(**kwargs):
        if kwargs["action"] == "upload_aborted":
            raise RuntimeError("event insert failed")
        return original_record_event(**kwargs)

    monkeypatch.setattr(client.app.state.catalog, "record_event", flaky_record_event)

    response = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b"<html>", "application/geo+json"),
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported media type"
    assert not any((storage_root / "_incoming").iterdir())


def test_upload_rejects_when_principal_concurrency_limit_is_reached(client, manifest_bytes_factory):
    client.app.state.settings = replace(client.app.state.settings, max_concurrent_uploads_per_principal=1)
    assert client.app.state.upload_limiter.acquire("vasnas", 1)

    try:
        response = client.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer vasnas-token"},
            data={"dataset_key": "smoke-slice"},
            files={
                "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
                "files": ("smoke_slice.geojson", b"{}", "application/geo+json"),
            },
        )
    finally:
        client.app.state.upload_limiter.release("vasnas")

    assert response.status_code == 429
    assert response.json()["detail"] == "Too many concurrent uploads"


def test_upload_retract_and_auth_failure_are_recorded_as_events(client, manifest_bytes_factory, db_path):
    upload = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token", "User-Agent": "pytest"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )
    assert upload.status_code == 200

    version_id = upload.json()["version_id"]
    retract = client.post(
        f"/v1/datasets/smoke-slice/versions/{version_id}/retract",
        headers={"Authorization": "Bearer vasnas-token"},
        json={"reason": "bad input"},
    )
    assert retract.status_code == 200

    client.get("/v1/me", headers={"Authorization": "Bearer nope"})

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY event_id")]

    actions = [row["action"] for row in rows]
    assert "upload_started" in actions
    assert "upload_committed" in actions
    assert "retracted" in actions
    assert "auth_failure" in actions
    committed = next(row for row in rows if row["action"] == "upload_committed")
    assert committed["actor_principal_id"] == "vasnas"
    assert committed["token_id"] == "vasnas-test"
    assert committed["dataset_key"] == "smoke-slice"
    assert committed["version_id"] == version_id


def test_metrics_report_upload_and_event_counts(client, manifest_bytes_factory):
    upload = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )
    assert upload.status_code == 200

    response = client.get("/v1/metrics", headers={"Authorization": "Bearer browser-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["versions"]["committed"] == 1
    assert body["files"]["count"] == 1
    assert body["files"]["bytes"] == len(b'{"type":"FeatureCollection"}')
    assert body["events"]["upload_committed"] == 1


def test_retracted_bytes_still_count_toward_quota(client, manifest_bytes_factory, db_path):
    upload = client.post(
        "/v1/datasets",
        headers={"Authorization": "Bearer vasnas-token"},
        data={"dataset_key": "smoke-slice"},
        files={
            "manifest": ("manifest.json", manifest_bytes_factory(), "application/json"),
            "files": ("smoke_slice.geojson", b'{"type":"FeatureCollection"}', "application/geo+json"),
        },
    )
    assert upload.status_code == 200

    client.post(
        f"/v1/datasets/smoke-slice/versions/{upload.json()['version_id']}/retract",
        headers={"Authorization": "Bearer vasnas-token"},
    )

    with sqlite3.connect(db_path) as conn:
        usage = conn.execute("SELECT stored_bytes FROM principal_usage WHERE principal_id = 'vasnas'").fetchone()[0]

    assert usage == len(b'{"type":"FeatureCollection"}')
