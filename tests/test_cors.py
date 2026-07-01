from __future__ import annotations

from fastapi.testclient import TestClient


def test_cors_headers_are_absent_by_default(client):
    response = client.get(
        "/v1/datasets",
        headers={
            "Authorization": "Bearer browser-token",
            "Origin": "http://localhost:5175",
        },
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_configured_cors_allows_browse_preflight_without_auth(monkeypatch, storage_root, db_path, tokens_json):
    monkeypatch.setenv("DTCC_UPLOAD_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DTCC_UPLOAD_DB_PATH", str(db_path))
    monkeypatch.setenv("DTCC_UPLOAD_TOKENS_JSON", tokens_json)
    monkeypatch.setenv("DTCC_UPLOAD_CORS_ORIGINS_JSON", '["http://localhost:5175"]')

    from dtcc_upload.app import create_app

    with TestClient(create_app(), raise_server_exceptions=True) as test_client:
        response = test_client.options(
            "/v1/datasets",
            headers={
                "Origin": "http://localhost:5175",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization, if-none-match, range",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5175"
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "if-none-match" in allowed_headers
    assert "range" in allowed_headers


def test_configured_cors_exposes_download_headers(monkeypatch, storage_root, db_path, tokens_json):
    monkeypatch.setenv("DTCC_UPLOAD_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DTCC_UPLOAD_DB_PATH", str(db_path))
    monkeypatch.setenv("DTCC_UPLOAD_TOKENS_JSON", tokens_json)
    monkeypatch.setenv("DTCC_UPLOAD_CORS_ORIGINS_JSON", '["http://localhost:5175"]')

    from dtcc_upload.app import create_app

    with TestClient(create_app(), raise_server_exceptions=True) as test_client:
        response = test_client.get(
            "/v1/datasets",
            headers={
                "Authorization": "Bearer browser-token",
                "Origin": "http://localhost:5175",
            },
        )

    exposed = {header.strip().lower() for header in response.headers["access-control-expose-headers"].split(",")}
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5175"
    assert {"etag", "content-disposition", "accept-ranges", "content-range"} <= exposed
