from __future__ import annotations

import hashlib
import json

from dtcc_upload.hash_utils import file_set_sha256, sha256_bytes


def test_sha256_bytes_is_hex_digest():
    assert sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_file_set_hash_is_order_independent():
    first = file_set_sha256(
        [
            {"path": "b.txt", "size": 2, "sha256": "b" * 64},
            {"path": "a.txt", "size": 1, "sha256": "a" * 64},
        ]
    )
    second = file_set_sha256(
        [
            {"path": "a.txt", "size": 1, "sha256": "a" * 64},
            {"path": "b.txt", "size": 2, "sha256": "b" * 64},
        ]
    )
    assert first == second


def test_file_set_hash_uses_normalized_compact_sorted_json_payload():
    records = [
        {"path": "b.txt", "size": "2", "sha256": "b" * 64, "ignored": "value"},
        {"path": "a.txt", "size": 1.0, "sha256": "a" * 64},
    ]
    normalized = [
        {"path": "a.txt", "size": 1, "sha256": "a" * 64},
        {"path": "b.txt", "size": 2, "sha256": "b" * 64},
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert file_set_sha256(records) == hashlib.sha256(payload).hexdigest()
