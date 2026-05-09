from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException, Response, status
from fastapi.responses import StreamingResponse


def etag_for_sha256(digest: str) -> str:
    return f'"{digest}"'


def not_modified_if_match(if_none_match: str | None, digest: str) -> Response | None:
    etag = etag_for_sha256(digest)
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return None


def parse_single_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    if range_header is None:
        return None
    invalid_headers = {"Content-Range": f"bytes */{size}"}
    if not range_header.startswith("bytes=") or "," in range_header:
        raise HTTPException(
            status_code=416,
            detail="Only single byte ranges are supported",
            headers=invalid_headers,
        )

    spec = range_header[len("bytes=") :]
    if "-" not in spec:
        raise HTTPException(status_code=416, detail="Invalid range", headers=invalid_headers)
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        if not end_text.isdigit():
            raise HTTPException(status_code=416, detail="Invalid range", headers=invalid_headers)
        suffix_length = int(end_text)
        if suffix_length <= 0 or size <= 0:
            raise HTTPException(status_code=416, detail="Invalid range", headers=invalid_headers)
        start = max(size - suffix_length, 0)
        return start, size - 1
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        raise HTTPException(status_code=416, detail="Invalid range", headers=invalid_headers)

    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise HTTPException(status_code=416, detail="Invalid range", headers=invalid_headers)
    end = min(end, size - 1)
    return start, end


def _open_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _iter_file_bytes(
    path: Path,
    *,
    start: int = 0,
    end: int | None = None,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    fd = _open_no_follow(path)
    with os.fdopen(fd, "rb", closefd=True) as handle:
        handle.seek(start)
        remaining = None if end is None else end - start + 1
        while remaining is None or remaining > 0:
            size = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = handle.read(size)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def bytes_response(
    payload: bytes,
    *,
    media_type: str,
    sha256: str,
    disposition: str,
    filename: str,
    range_header: str | None = None,
    if_none_match: str | None = None,
) -> Response:
    cached = not_modified_if_match(if_none_match, sha256)
    if cached is not None:
        return cached

    headers = {
        "ETag": etag_for_sha256(sha256),
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Accept-Ranges": "bytes",
    }
    byte_range = parse_single_range(range_header, len(payload))
    if byte_range is None:
        headers["Content-Length"] = str(len(payload))
        return Response(payload, media_type=media_type, headers=headers)

    start, end = byte_range
    body = payload[start : end + 1]
    headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
    headers["Content-Length"] = str(len(body))
    return Response(body, status_code=206, media_type=media_type, headers=headers)


def file_response(
    path: Path,
    *,
    media_type: str,
    sha256: str,
    disposition: str,
    filename: str,
    range_header: str | None = None,
    if_none_match: str | None = None,
) -> Response:
    cached = not_modified_if_match(if_none_match, sha256)
    if cached is not None:
        return cached

    size = path.stat().st_size
    headers = {
        "ETag": etag_for_sha256(sha256),
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Accept-Ranges": "bytes",
    }
    byte_range = parse_single_range(range_header, size)
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(_iter_file_bytes(path), media_type=media_type, headers=headers)

    start, end = byte_range
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        _iter_file_bytes(path, start=start, end=end),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )
