from __future__ import annotations


class MediaTypeError(ValueError):
    pass


BLOCKED_MEDIA_TYPES = {"text/html", "image/svg+xml"}
JSON_MEDIA_TYPES = {"application/json", "application/geo+json"}
PARQUET_MEDIA_TYPES = {"application/parquet", "application/vnd.apache.parquet"}
CSV_MEDIA_TYPES = {"text/csv", "application/csv"}


def is_json_media_type(value: str) -> bool:
    media_type = value.lower().split(";", 1)[0].strip()
    return media_type in JSON_MEDIA_TYPES or media_type.endswith("+json")


def sniff_media_type(sample: bytes, declared_media_type: str) -> str:
    declared = declared_media_type.lower().split(";", 1)[0].strip()
    stripped = sample.lstrip()
    lower = stripped[:512].lower()

    if lower.startswith(b"<!doctype html") or lower.startswith(b"<html") or lower.startswith(b"<script"):
        return "text/html"
    if lower.startswith(b"<svg") or (lower.startswith(b"<?xml") and b"<svg" in lower):
        return "image/svg+xml"
    if sample.startswith(b"PAR1"):
        return "application/vnd.apache.parquet"
    if stripped[:1] in {b"{", b"["}:
        return declared if is_json_media_type(declared) else "application/json"
    if b"\x00" in sample:
        return "application/octet-stream"

    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"

    first_line = sample.splitlines()[0] if sample.splitlines() else b""
    if b"," in first_line:
        return "text/csv"
    return "text/plain"


def validate_media_type(declared_media_type: str, sniffed_media_type: str) -> str:
    declared = declared_media_type.lower().split(";", 1)[0].strip()
    sniffed = sniffed_media_type.lower().split(";", 1)[0].strip()

    if declared in BLOCKED_MEDIA_TYPES or sniffed in BLOCKED_MEDIA_TYPES:
        raise MediaTypeError("Unsupported media type")
    if is_json_media_type(declared) and not is_json_media_type(sniffed):
        raise MediaTypeError("Declared media type does not match file content")
    if declared in PARQUET_MEDIA_TYPES and sniffed != "application/vnd.apache.parquet":
        raise MediaTypeError("Declared media type does not match file content")
    if declared in CSV_MEDIA_TYPES and sniffed not in {"text/csv", "text/plain"}:
        raise MediaTypeError("Declared media type does not match file content")
    return sniffed
