from __future__ import annotations

from threading import Lock

from fastapi import HTTPException

from dtcc_upload.config import Settings


class UploadConcurrencyLimiter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: dict[str, int] = {}

    def acquire(self, principal_id: str, max_concurrent: int) -> bool:
        limit = max(1, max_concurrent)
        with self._lock:
            current = self._active.get(principal_id, 0)
            if current >= limit:
                return False
            self._active[principal_id] = current + 1
            return True

    def release(self, principal_id: str) -> None:
        with self._lock:
            current = self._active.get(principal_id, 0)
            if current <= 1:
                self._active.pop(principal_id, None)
            else:
                self._active[principal_id] = current - 1


def enforce_upload_counts(
    settings: Settings,
    *,
    file_count: int,
    total_parts: int,
    non_file_fields: int | None = None,
) -> None:
    if file_count > settings.max_files_per_upload:
        raise HTTPException(status_code=413, detail="Too many files")
    if non_file_fields is not None and non_file_fields > settings.max_non_file_fields:
        raise HTTPException(status_code=413, detail="Too many form fields")
    if total_parts > settings.max_total_parts:
        raise HTTPException(status_code=413, detail="Too many multipart parts")


def enforce_upload_bytes(
    settings: Settings,
    *,
    file_bytes: int | None = None,
    total_bytes: int | None = None,
) -> None:
    if file_bytes is not None and file_bytes > settings.max_file_bytes:
        raise HTTPException(status_code=413, detail="File too large")
    if total_bytes is not None and total_bytes > settings.max_total_upload_bytes:
        raise HTTPException(status_code=413, detail="Upload too large")
