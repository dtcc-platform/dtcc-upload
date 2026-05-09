from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_set_sha256(records: Iterable[Mapping[str, object]]) -> str:
    normalized = sorted(
        (
            {
                "path": str(record["path"]),
                "size": int(record["size"]),
                "sha256": str(record["sha256"]),
            }
            for record in records
        ),
        key=lambda item: item["path"],
    )
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)
