from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dtcc_upload.catalog import Catalog
from dtcc_upload.hash_utils import sha256_bytes
from dtcc_upload.storage import Storage


def _pending_values(
    *,
    dataset_key: str = "smoke-slice",
    version_id: str = "v1",
    upload_id: str = "upload-1",
    manifest_sha256: str = "a" * 64,
    file_set_sha256: str = "b" * 64,
    total_bytes: int = 123,
) -> dict[str, object]:
    return {
        "dataset_key": dataset_key,
        "version_id": version_id,
        "version_number": int(version_id.removeprefix("v") or "1"),
        "principal_id": "vasnas",
        "manifest_sha256": manifest_sha256,
        "file_set_sha256": file_set_sha256,
        "manifest_size": 100,
        "format": "geojson",
        "media_type": "application/geo+json",
        "data_kind": "vector",
        "product": "slice",
        "title": "Smoke",
        "bounds_json": "[0,0,1,1]",
        "total_bytes": total_bytes,
        "file_count": 1,
        "request_id": f"req-{version_id}",
        "upload_id": upload_id,
    }


def _catalog_with_dataset(db_path: Path) -> Catalog:
    catalog = Catalog(db_path)
    catalog.init_schema()
    catalog.claim_dataset("smoke-slice", "vasnas")
    return catalog


def _set_created_at(db_path: Path, dataset_key: str, version_id: str, created_at: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE versions SET created_at = ? WHERE dataset_key = ? AND version_id = ?",
            (created_at, dataset_key, version_id),
        )


def _write_ready(path: Path, *, dataset_key: str, version_id: str, manifest_sha256: str, file_set_sha256: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "commit.ready").write_text(
        json.dumps(
            {
                "dataset_key": dataset_key,
                "version_id": version_id,
                "manifest_sha256": manifest_sha256,
                "file_set_sha256": file_set_sha256,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _request_hash(dataset_key: str, manifest_sha256: str, file_set_sha256: str) -> str:
    return sha256_bytes(
        json.dumps(
            {
                "dataset_key": dataset_key,
                "manifest_sha256": manifest_sha256,
                "file_set_sha256": file_set_sha256,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _idempotency_row(db_path: Path, key: str) -> dict[str, object] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row is not None else None


def test_recover_pending_commits_pending_finalized_version(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    pending = catalog.insert_pending_version(**_pending_values())
    storage = Storage(storage_root)
    _write_ready(
        storage.version_dir("smoke-slice", "v1"),
        dataset_key="smoke-slice",
        version_id="v1",
        manifest_sha256=pending["manifest_sha256"],
        file_set_sha256=pending["file_set_sha256"],
    )

    recovered = storage.recover_pending(catalog)

    assert recovered == 1
    assert catalog.get_version("smoke-slice", "v1")["status"] == "committed"
    assert catalog.get_dataset("smoke-slice")["latest_committed_version_id"] == "v1"


def test_recover_pending_finalizes_staged_ready_upload(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    pending = catalog.insert_pending_version(**_pending_values(upload_id="upload-ready"))
    storage = Storage(storage_root)
    staged = storage.create_staging_dir("upload-ready")
    (staged / "manifest.json").write_text("{}", encoding="utf-8")
    _write_ready(
        staged,
        dataset_key="smoke-slice",
        version_id="v1",
        manifest_sha256=pending["manifest_sha256"],
        file_set_sha256=pending["file_set_sha256"],
    )

    recovered = storage.recover_pending(catalog)

    final_dir = storage.version_dir("smoke-slice", "v1")
    assert recovered == 1
    assert final_dir.exists()
    assert not staged.exists()
    assert catalog.get_version("smoke-slice", "v1")["status"] == "committed"


def test_cleanup_stale_incoming_removes_orphan_dirs(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    catalog.insert_pending_version(**_pending_values(upload_id="keep-upload"))
    storage = Storage(storage_root)
    keep = storage.create_staging_dir("keep-upload")
    orphan = storage.create_staging_dir("orphan-upload")
    old = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc).timestamp()
    os.utime(orphan, (old, old))
    now = old + 3600

    removed = storage.cleanup_stale_incoming(catalog, max_age_seconds=60, now=now)

    assert removed == 1
    assert keep.exists()
    assert not orphan.exists()


def test_cleanup_stale_pending_finals_removes_stale_final_dir_and_pending_row(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    catalog.insert_pending_version(**_pending_values())
    _set_created_at(db_path, "smoke-slice", "v1", "2026-05-09 08:00:00")
    storage = Storage(storage_root)
    final_dir = storage.version_dir("smoke-slice", "v1")
    _write_ready(
        final_dir,
        dataset_key="smoke-slice",
        version_id="v1",
        manifest_sha256="a" * 64,
        file_set_sha256="b" * 64,
    )
    now = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc).timestamp()

    removed = storage.cleanup_stale_pending_finals(catalog, max_age_seconds=60, now=now)

    assert removed == 1
    assert catalog.get_version("smoke-slice", "v1") is None
    assert not final_dir.exists()


def test_cleanup_stale_pending_finals_deletes_stale_pending_row_with_no_artifacts(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    catalog.insert_pending_version(**_pending_values(upload_id="missing-upload"))
    _set_created_at(db_path, "smoke-slice", "v1", "2026-05-09 08:00:00")
    storage = Storage(storage_root)
    now = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc).timestamp()

    removed = storage.cleanup_stale_pending_finals(catalog, max_age_seconds=60, now=now)

    assert removed == 1
    assert catalog.get_version("smoke-slice", "v1") is None


def test_cleanup_stale_pending_finals_deletes_staged_pending_without_ready(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    catalog.insert_pending_version(**_pending_values(upload_id="half-upload"))
    _set_created_at(db_path, "smoke-slice", "v1", "2026-05-09 08:00:00")
    storage = Storage(storage_root)
    staged = storage.create_staging_dir("half-upload")
    (staged / "manifest.json").write_text("{}", encoding="utf-8")
    now = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc).timestamp()

    removed = storage.cleanup_stale_pending_finals(catalog, max_age_seconds=60, now=now)

    assert removed == 1
    assert catalog.get_version("smoke-slice", "v1") is None
    assert not staged.exists()


def test_recovery_honors_principal_quota(db_path, storage_root):
    catalog = _catalog_with_dataset(db_path)
    pending = catalog.insert_pending_version(**_pending_values(total_bytes=123))
    storage = Storage(storage_root)
    _write_ready(
        storage.version_dir("smoke-slice", "v1"),
        dataset_key="smoke-slice",
        version_id="v1",
        manifest_sha256=pending["manifest_sha256"],
        file_set_sha256=pending["file_set_sha256"],
    )

    recovered = storage.recover_pending(catalog, max_stored_bytes_per_principal=1)

    assert recovered == 0
    assert catalog.get_version("smoke-slice", "v1")["status"] == "pending"


def test_reconcile_idempotency_completes_matching_committed_version(db_path):
    catalog = _catalog_with_dataset(db_path)
    version = catalog.insert_pending_version(**_pending_values())
    catalog.insert_file_record(
        dataset_key="smoke-slice",
        version_id="v1",
        path="smoke_slice.geojson",
        original_filename="smoke_slice.geojson",
        size=2,
        sha256="c" * 64,
        media_type="application/geo+json",
        sniffed_media_type="application/geo+json",
    )
    catalog.commit_version("smoke-slice", "v1")
    request_hash = _request_hash(
        "smoke-slice",
        str(version["manifest_sha256"]),
        str(version["file_set_sha256"]),
    )
    catalog.reserve_idempotency_key("vasnas", "smoke-slice", "retry-1", request_hash)

    result = catalog.reconcile_idempotency_keys(max_age_seconds=60)

    row = _idempotency_row(db_path, "retry-1")
    assert result == {"completed": 1, "deleted": 0}
    assert row["status"] == "committed"
    assert json.loads(row["response_json"])["version_id"] == "v1"


def test_reconcile_idempotency_deletes_stale_orphan_reservation(db_path):
    catalog = _catalog_with_dataset(db_path)
    catalog.reserve_idempotency_key("vasnas", "smoke-slice", "retry-1", "orphan-hash")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE idempotency_keys SET created_at = '2026-05-09 08:00:00' WHERE idempotency_key = ?",
            ("retry-1",),
        )
    now = datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc).timestamp()

    result = catalog.reconcile_idempotency_keys(max_age_seconds=60, now=now)

    assert result == {"completed": 0, "deleted": 1}
    assert _idempotency_row(db_path, "retry-1") is None
