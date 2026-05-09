from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest


def _version_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0])


def _file_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])


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
