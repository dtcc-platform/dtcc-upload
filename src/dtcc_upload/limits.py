from __future__ import annotations

from fastapi import HTTPException

from dtcc_upload.config import Settings


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
