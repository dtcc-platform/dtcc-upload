from __future__ import annotations

import re
import unicodedata


WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}

WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
MAX_V2_PACKAGE_PATH_BYTES = 1024
MAX_V2_PACKAGE_PART_BYTES = 255


def validate_logical_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid file path")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise ValueError("Invalid file path")
    # v1 intentionally rejects dotfiles because file names become URL-visible artifacts.
    if normalized.startswith("/") or normalized.startswith("."):
        raise ValueError("Invalid file path")
    if "/" in normalized or "\\" in normalized or "\x00" in normalized:
        raise ValueError("Invalid file path")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("Invalid file path")
    if ".." in normalized:
        raise ValueError("Invalid file path")
    if len(normalized.encode("utf-8")) > 255:
        raise ValueError("Invalid file path")

    basename = normalized.rsplit(".", 1)[0].lower()
    if basename in WINDOWS_RESERVED_BASENAMES:
        raise ValueError("Invalid file path")
    return normalized


def validate_package_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid file path")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise ValueError("Invalid file path")
    if normalized.startswith("/") or normalized.startswith("."):
        raise ValueError("Invalid file path")
    if WINDOWS_DRIVE_RE.match(normalized):
        raise ValueError("Invalid file path")
    if "\\" in normalized or "\x00" in normalized or "//" in normalized:
        raise ValueError("Invalid file path")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("Invalid file path")
    if len(normalized.encode("utf-8")) > MAX_V2_PACKAGE_PATH_BYTES:
        raise ValueError("Invalid file path")

    parts = normalized.split("/")
    for part in parts:
        if not part or part in {".", ".."} or part.startswith("."):
            raise ValueError("Invalid file path")
        if len(part.encode("utf-8")) > MAX_V2_PACKAGE_PART_BYTES:
            raise ValueError("Invalid file path")
        basename = part.rsplit(".", 1)[0].lower()
        if basename in WINDOWS_RESERVED_BASENAMES:
            raise ValueError("Invalid file path")

    return normalized
