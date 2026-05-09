from __future__ import annotations

from fastapi.testclient import TestClient


def test_me_returns_canonical_principal(client):
    response = client.get("/v1/me", headers={"Authorization": "Bearer vasnas-token"})
    assert response.status_code == 200
    assert response.json() == {
        "principal_id": "vasnas",
        "token_id": "vasnas-test",
        "scopes": ["upload", "browse"],
    }


def test_missing_token_is_unauthorized(client):
    response = client.get("/v1/me")
    assert response.status_code == 401


def test_invalid_token_is_unauthorized(client):
    response = client.get("/v1/me", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_bearer_scheme_is_case_insensitive(client):
    response = client.get("/v1/me", headers={"Authorization": "bearer vasnas-token"})
    assert response.status_code == 200
    assert response.json()["principal_id"] == "vasnas"


def test_browse_token_has_only_browse_scope(client):
    response = client.get("/v1/me", headers={"Authorization": "Bearer browser-token"})
    assert response.status_code == 200
    assert response.json()["principal_id"] == "browser"
    assert response.json()["scopes"] == ["browse"]


def test_principal_aliases_return_canonical_identity(monkeypatch, storage_root, db_path):
    monkeypatch.setenv("DTCC_UPLOAD_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DTCC_UPLOAD_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DTCC_UPLOAD_TOKENS_JSON",
        '[{"token_id":"vbassn-test","principal_id":"vbassn","token":"vbassn-token","scopes":["upload","browse"]}]',
    )
    monkeypatch.setenv("DTCC_UPLOAD_PRINCIPAL_ALIASES_JSON", '{"vbassn":"vasnas"}')

    from dtcc_upload.app import create_app

    with TestClient(create_app(), raise_server_exceptions=True) as test_client:
        response = test_client.get("/v1/me", headers={"Authorization": "Bearer vbassn-token"})

    assert response.status_code == 200
    assert response.json()["principal_id"] == "vasnas"
