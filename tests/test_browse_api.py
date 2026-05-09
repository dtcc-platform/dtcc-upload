from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from dtcc_upload.catalog import Catalog


def test_browse_requires_auth(client):
    response = client.get("/v1/datasets")
    assert response.status_code == 401


def test_empty_browse_returns_empty_items(client):
    response = client.get("/v1/datasets", headers={"Authorization": "Bearer browser-token"})
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_browse_requires_browse_scope(monkeypatch, storage_root, db_path):
    monkeypatch.setenv("DTCC_UPLOAD_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DTCC_UPLOAD_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DTCC_UPLOAD_TOKENS_JSON",
        '[{"token_id":"upload-only","principal_id":"vasnas","token":"upload-only-token","scopes":["upload"]}]',
    )

    from dtcc_upload.app import create_app

    with TestClient(create_app(), raise_server_exceptions=True) as test_client:
        response = test_client.get("/v1/datasets", headers={"Authorization": "Bearer upload-only-token"})

    assert response.status_code == 403


def test_include_retracted_cursor_keeps_same_dataset_rows(client, db_path):
    catalog = Catalog(db_path)
    catalog.claim_dataset("smoke-slice", "vasnas")
    for version_number, version_id, status in [(1, "v1", "retracted"), (2, "v2", "committed")]:
        catalog.insert_pending_version(
            dataset_key="smoke-slice",
            version_id=version_id,
            version_number=version_number,
            principal_id="vasnas",
            manifest_sha256=str(version_number) * 64,
            file_set_sha256=str(version_number + 2) * 64,
            manifest_size=100,
            format="geojson",
            media_type="application/geo+json",
            data_kind="vector",
            product="slice",
            title="Smoke",
            bounds_json="[0,0,1,1]",
            total_bytes=123,
            file_count=1,
            request_id=f"req-{version_id}",
            upload_id=f"upload-{version_id}",
        )
        catalog.commit_version("smoke-slice", version_id)
        if status == "retracted":
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE versions SET status = 'retracted' WHERE dataset_key = ? AND version_id = ?",
                    ("smoke-slice", version_id),
                )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE versions
            SET committed_at = '2026-05-09 12:00:00'
            WHERE dataset_key = ?
            """,
            ("smoke-slice",),
        )

    first_page = client.get(
        "/v1/datasets?include_retracted=true&limit=1",
        headers={"Authorization": "Bearer vasnas-token"},
    )
    assert first_page.status_code == 200
    assert first_page.json()["next_cursor"] is not None
    second_page = client.get(
        f"/v1/datasets?include_retracted=true&limit=1&cursor={first_page.json()['next_cursor']}",
        headers={"Authorization": "Bearer vasnas-token"},
    )

    assert second_page.status_code == 200
    assert {first_page.json()["items"][0]["version_id"], second_page.json()["items"][0]["version_id"]} == {
        "v1",
        "v2",
    }
