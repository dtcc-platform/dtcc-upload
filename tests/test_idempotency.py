from __future__ import annotations

import json
import sqlite3

import pytest

from dtcc_upload.hash_utils import file_set_sha256, sha256_bytes
from dtcc_upload.storage import Storage


def _version_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0])


def _idempotency_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0])


def _upload(client, *, dataset_key: str, body: bytes, idempotency_key: str, manifest_bytes: bytes):
    return client.post(
        "/v1/datasets",
        headers={
            "Authorization": "Bearer vasnas-token",
            "Idempotency-Key": idempotency_key,
        },
        data={"dataset_key": dataset_key},
        files={
            "manifest": ("manifest.json", manifest_bytes, "application/json"),
            "files": ("smoke_slice.geojson", body, "application/geo+json"),
        },
    )


def test_pending_idempotency_key_returns_in_progress(client, manifest_bytes_factory, db_path):
    body = b'{"type":"FeatureCollection","features":[]}'
    manifest_bytes = manifest_bytes_factory()
    manifest_sha = sha256_bytes(manifest_bytes)
    file_set_sha = file_set_sha256(
        [
            {
                "path": "smoke_slice.geojson",
                "size": len(body),
                "sha256": sha256_bytes(body),
            }
        ]
    )
    request_hash = sha256_bytes(
        json.dumps(
            {
                "dataset_key": "smoke-slice",
                "manifest_sha256": manifest_sha,
                "file_set_sha256": file_set_sha,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    client.app.state.catalog.reserve_idempotency_key(
        "vasnas",
        "smoke-slice",
        "retry-1",
        request_hash,
    )

    response = _upload(
        client,
        dataset_key="smoke-slice",
        body=body,
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Idempotency-Key is already in progress"
    assert _version_count(db_path) == 0


def test_same_idempotency_key_replays_same_response(client, manifest_bytes_factory, db_path):
    first = _upload(
        client,
        dataset_key="smoke-slice",
        body=b'{"type":"FeatureCollection","features":[]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )
    second = _upload(
        client,
        dataset_key="smoke-slice",
        body=b'{"type":"FeatureCollection","features":[]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _version_count(db_path) == 1


def test_same_idempotency_key_with_different_body_returns_conflict_and_keeps_one_version(
    client,
    manifest_bytes_factory,
    db_path,
):
    first = _upload(
        client,
        dataset_key="smoke-slice",
        body=b'{"type":"FeatureCollection","features":[]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )
    second = _upload(
        client,
        dataset_key="smoke-slice",
        body=b'{"type":"FeatureCollection","features":[{"type":"Feature"}]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "Idempotency-Key conflicts with previous request"
    assert _version_count(db_path) == 1


def test_same_idempotency_key_is_scoped_to_dataset(client, manifest_bytes_factory, db_path):
    first = _upload(
        client,
        dataset_key="smoke-slice",
        body=b'{"type":"FeatureCollection","features":[]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )
    second = _upload(
        client,
        dataset_key="smoke-slice-2",
        body=b'{"type":"FeatureCollection","features":[]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["dataset_key"] == "smoke-slice"
    assert second.json()["dataset_key"] == "smoke-slice-2"
    assert _version_count(db_path) == 2


def test_idempotency_reservation_is_released_after_failed_upload_cleanup(
    client,
    manifest_bytes_factory,
    db_path,
    monkeypatch,
):
    original_finalize = Storage.finalize
    calls = {"count": 0}

    def fail_once(self, staged, dataset_key, version_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("finalize failed")
        return original_finalize(self, staged, dataset_key, version_id)

    monkeypatch.setattr("dtcc_upload.app.Storage.finalize", fail_once)

    with pytest.raises(RuntimeError, match="finalize failed"):
        _upload(
            client,
            dataset_key="smoke-slice",
            body=b'{"type":"FeatureCollection","features":[]}',
            idempotency_key="retry-1",
            manifest_bytes=manifest_bytes_factory(),
        )
    assert _version_count(db_path) == 0
    assert _idempotency_count(db_path) == 0

    second = _upload(
        client,
        dataset_key="smoke-slice",
        body=b'{"type":"FeatureCollection","features":[]}',
        idempotency_key="retry-1",
        manifest_bytes=manifest_bytes_factory(),
    )

    assert second.status_code == 200
    assert _version_count(db_path) == 1
