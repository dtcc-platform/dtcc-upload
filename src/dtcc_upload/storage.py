from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from dtcc_upload.validation import validate_dataset_key, validate_logical_path


logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.incoming_root = self.root / "_incoming"
        self.datasets_root = self.root / "datasets"

    def create_staging_dir(self, upload_id: str) -> Path:
        safe_upload_id = self._validate_path_component(upload_id)
        staged = self.incoming_root / safe_upload_id
        self._mkdir_with_parent_fsync(staged / "files", exist_ok=False)
        self._fsync_dir(staged)
        return staged

    def version_dir(self, dataset_key: str, version_id: str) -> Path:
        return self.datasets_root / validate_dataset_key(dataset_key) / self._validate_path_component(version_id)

    def write_manifest(self, staged: Path, payload: bytes) -> Path:
        path = staged / "manifest.json"
        path.write_bytes(payload)
        self._fsync_file(path)
        self._fsync_dir(path.parent)
        return path

    def write_staged_file(
        self,
        staged: Path,
        logical_path: str,
        chunks: Iterable[bytes],
        *,
        sample_bytes: int = 4096,
    ) -> dict[str, object]:
        safe_name = validate_logical_path(logical_path)
        path = staged / "files" / safe_name
        digest = hashlib.sha256()
        size = 0
        sample = bytearray()

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    digest.update(chunk)
                    size += len(chunk)
                    if len(sample) < sample_bytes:
                        sample.extend(chunk[: sample_bytes - len(sample)])
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)

        self._fsync_dir(path.parent)
        return {
            "path": safe_name,
            "size": size,
            "sha256": digest.hexdigest(),
            "sample": bytes(sample),
        }

    def write_commit_ready(self, staged: Path, payload: dict[str, str]) -> Path:
        path = staged / "commit.ready"
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        self._fsync_file(path)
        self._fsync_dir(path.parent)
        return path

    def finalize(self, staged: Path, dataset_key: str, version_id: str) -> Path:
        final = self.version_dir(dataset_key, version_id)
        self._assert_under(staged, self.incoming_root)
        self._mkdir_with_parent_fsync(final.parent, exist_ok=True)
        self._fsync_dir(staged)
        os.replace(staged, final)
        self._fsync_dir(final.parent)
        return final

    def resolve_version_file(self, version_dir: Path, logical_path: str) -> Path:
        safe_name = validate_logical_path(logical_path)
        base = version_dir.resolve(strict=True)
        candidate = version_dir / "files" / safe_name
        if candidate.is_symlink():
            raise ValueError("Invalid file path")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError("Invalid file path") from exc
        return resolved

    def cleanup_staging(self, upload_id: str) -> None:
        shutil.rmtree(self.incoming_root / self._validate_path_component(upload_id), ignore_errors=True)

    def cleanup_version_dir(self, dataset_key: str, version_id: str) -> None:
        version_path = self.version_dir(dataset_key, version_id)
        shutil.rmtree(version_path, ignore_errors=True)
        try:
            version_path.parent.rmdir()
        except OSError:
            pass

    def recover_pending(self, catalog, max_stored_bytes_per_principal: int | None = None) -> int:
        recovered = 0
        for row in catalog.list_pending_versions():
            dataset_key = str(row["dataset_key"])
            version_id = str(row["version_id"])
            expected = {
                "dataset_key": dataset_key,
                "version_id": version_id,
                "manifest_sha256": str(row["manifest_sha256"]),
                "file_set_sha256": str(row["file_set_sha256"]),
            }

            final_dir = self.version_dir(dataset_key, version_id)
            final_ready = final_dir / "commit.ready"
            staged = self.incoming_root / self._validate_path_component(str(row["upload_id"]))
            staged_ready = staged / "commit.ready"

            if final_ready.exists():
                if self._commit_ready_matches(final_ready, expected):
                    recovered += self._commit_pending(
                        catalog,
                        dataset_key,
                        version_id,
                        max_stored_bytes_per_principal=max_stored_bytes_per_principal,
                    )
                continue

            if staged_ready.exists() and self._commit_ready_matches(staged_ready, expected):
                self.finalize(staged, dataset_key, version_id)
                recovered += self._commit_pending(
                    catalog,
                    dataset_key,
                    version_id,
                    max_stored_bytes_per_principal=max_stored_bytes_per_principal,
                )

        return recovered

    def cleanup_stale_incoming(self, catalog, max_age_seconds: int, now: float | None = None) -> int:
        if not self.incoming_root.exists():
            return 0

        active_upload_ids = {
            self._validate_path_component(str(row["upload_id"]))
            for row in catalog.list_pending_versions()
        }
        current_time = datetime.now(timezone.utc).timestamp() if now is None else now
        removed = 0

        for child in self.incoming_root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            upload_id = self._validate_path_component(child.name)
            if upload_id in active_upload_ids:
                continue
            if current_time - child.stat().st_mtime <= max_age_seconds:
                continue
            shutil.rmtree(child)
            self._fsync_dir(self.incoming_root)
            removed += 1

        return removed

    def cleanup_stale_pending_finals(self, catalog, max_age_seconds: int, now: float | None = None) -> int:
        current_time = datetime.now(timezone.utc).timestamp() if now is None else now
        removed = 0

        for row in catalog.list_pending_versions():
            created_at = self._created_at_timestamp(str(row["created_at"]))
            if current_time - created_at <= max_age_seconds:
                continue

            dataset_key = str(row["dataset_key"])
            version_id = str(row["version_id"])
            upload_id = self._validate_path_component(str(row["upload_id"]))
            final_dir = self.version_dir(dataset_key, version_id)
            staged = self.incoming_root / upload_id

            if final_dir.exists():
                self.cleanup_version_dir(dataset_key, version_id)
                catalog.delete_pending_version(dataset_key, version_id)
                removed += 1
            elif staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
                self._fsync_dir(self.incoming_root)
                catalog.delete_pending_version(dataset_key, version_id)
                removed += 1
            elif not staged.exists():
                catalog.delete_pending_version(dataset_key, version_id)
                removed += 1

        return removed

    def _created_at_timestamp(self, value: str) -> float:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()

    def _commit_pending(
        self,
        catalog,
        dataset_key: str,
        version_id: str,
        *,
        max_stored_bytes_per_principal: int | None,
    ) -> int:
        try:
            catalog.commit_version(
                dataset_key,
                version_id,
                max_stored_bytes_per_principal=max_stored_bytes_per_principal,
            )
        except RuntimeError as exc:
            logger.warning("Failed to recover pending version %s/%s: %s", dataset_key, version_id, exc)
            return 0
        return 1

    def _commit_ready_matches(self, path: Path, expected: dict[str, str]) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return all(payload.get(key) == value for key, value in expected.items())

    def _validate_path_component(self, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Invalid path component")
        if not value or value in {".", ".."} or value.startswith("."):
            raise ValueError("Invalid path component")
        if "/" in value or "\\" in value or "\x00" in value or ".." in value:
            raise ValueError("Invalid path component")
        if any(ord(char) < 32 for char in value):
            raise ValueError("Invalid path component")
        return value

    def _assert_under(self, child: Path, base: Path) -> None:
        resolved_base = base.resolve(strict=True)
        resolved_child = child.resolve(strict=True)
        try:
            resolved_child.relative_to(resolved_base)
        except ValueError as exc:
            raise ValueError("Invalid path component") from exc

    def _mkdir_with_parent_fsync(self, path: Path, *, exist_ok: bool) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        path.mkdir(parents=True, exist_ok=exist_ok)
        for created in reversed(missing):
            self._fsync_dir(created.parent)

    def _fsync_file(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fsync_dir(self, path: Path) -> None:
        if not path.exists():
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
