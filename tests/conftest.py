from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "catalog.sqlite3"


@pytest.fixture
def tokens_json() -> str:
    return (
        "["
        '{"token_id":"logg-test","principal_id":"logg","token":"logg-token","scopes":["upload","browse"]},'
        '{"token_id":"vasnas-test","principal_id":"vasnas","token":"vasnas-token","scopes":["upload","browse"]},'
        '{"token_id":"browser-test","principal_id":"browser","token":"browser-token","scopes":["browse"]}'
        "]"
    )


@pytest.fixture
def manifest_bytes_factory():
    def build(filename: str = "smoke_slice.geojson") -> bytes:
        import json

        return json.dumps(
            {
                "name": "smoke",
                "file": filename,
                "format": "geojson",
                "media_type": "application/geo+json",
                "data_kind": "vector",
                "product": "slice",
                "bounds": [0, 0, 1, 1],
                "parameters": {"product": "slice"},
            }
        ).encode("utf-8")

    return build


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, storage_root: Path, db_path: Path, tokens_json: str):
    monkeypatch.setenv("DTCC_UPLOAD_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DTCC_UPLOAD_DB_PATH", str(db_path))
    monkeypatch.setenv("DTCC_UPLOAD_TOKENS_JSON", tokens_json)

    from dtcc_upload.app import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
