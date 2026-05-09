from __future__ import annotations

import hashlib
import json

import pytest

from dtcc_upload.storage import Storage


def test_stage_and_finalize_version(storage_root):
    storage = Storage(storage_root)
    staged = storage.create_staging_dir("upload-1")
    manifest_bytes = b'{"name":"smoke"}'
    manifest_path = storage.write_manifest(staged, manifest_bytes)
    file_info = storage.write_staged_file(
        staged,
        "smoke_slice.geojson",
        [b'{"type":', b'"FeatureCollection"}'],
    )
    storage.write_commit_ready(
        staged,
        {
            "dataset_key": "smoke-slice",
            "version_id": "v1",
            "manifest_sha256": "a" * 64,
            "file_set_sha256": "b" * 64,
        },
    )

    final_dir = storage.finalize(staged, "smoke-slice", "v1")

    file_bytes = b'{"type":"FeatureCollection"}'
    assert final_dir == storage_root / "datasets" / "smoke-slice" / "v1"
    assert manifest_path == staged / "manifest.json"
    assert (final_dir / "manifest.json").read_bytes() == manifest_bytes
    assert (final_dir / "files" / "smoke_slice.geojson").read_bytes() == file_bytes
    assert file_info == {
        "path": "smoke_slice.geojson",
        "size": len(file_bytes),
        "sha256": hashlib.sha256(file_bytes).hexdigest(),
        "sample": file_bytes,
    }
    assert json.loads((final_dir / "commit.ready").read_text(encoding="utf-8"))["version_id"] == "v1"
    assert not staged.exists()


def test_write_staged_file_limits_sample_bytes(storage_root):
    storage = Storage(storage_root)
    staged = storage.create_staging_dir("upload-1")

    file_info = storage.write_staged_file(staged, "smoke_slice.geojson", [b"abcdef", b"gh"], sample_bytes=4)

    assert file_info["sample"] == b"abcd"
    assert file_info["size"] == 8


def test_resolve_version_file_rejects_escape(storage_root):
    storage = Storage(storage_root)
    final_dir = storage.version_dir("smoke-slice", "v1")
    final_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="Invalid file path"):
        storage.resolve_version_file(final_dir, "../x")


def test_cleanup_rejects_path_traversal(storage_root):
    storage = Storage(storage_root)
    protected = storage_root / "datasets"
    protected.mkdir(parents=True)
    sentinel = protected / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid path component"):
        storage.cleanup_staging("../datasets")

    assert sentinel.exists()


def test_cleanup_version_dir_rejects_path_traversal(storage_root):
    storage = Storage(storage_root)
    protected = storage_root / "outside"
    protected.mkdir(parents=True)
    sentinel = protected / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid dataset_key"):
        storage.cleanup_version_dir("../../outside", "v1")

    assert sentinel.exists()


def test_resolve_version_file_rejects_symlinked_files_escape(storage_root):
    storage = Storage(storage_root)
    final_dir = storage.version_dir("smoke-slice", "v1")
    final_dir.mkdir(parents=True)
    outside = storage_root / "outside"
    outside.mkdir()
    (outside / "smoke_slice.geojson").write_text("escaped", encoding="utf-8")
    (final_dir / "files").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Invalid file path"):
        storage.resolve_version_file(final_dir, "smoke_slice.geojson")
