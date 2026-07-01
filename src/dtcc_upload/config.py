from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TokenConfig:
    token_id: str
    principal_id: str
    token: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    storage_root: Path
    db_path: Path
    tokens: tuple[TokenConfig, ...]
    principal_aliases: dict[str, str] = field(default_factory=dict)
    cors_origins: tuple[str, ...] = ()
    bind_host: str = "127.0.0.1"
    max_manifest_bytes: int = 1 * 1024 * 1024
    max_files_per_upload: int = 64
    max_non_file_fields: int = 16
    max_total_parts: int = 128
    max_file_bytes: int = 100 * 1024 * 1024
    max_total_upload_bytes: int = 500 * 1024 * 1024
    max_versions_per_dataset: int = 1000
    max_concurrent_uploads_per_principal: int = 2
    max_stored_bytes_per_principal: int = 10 * 1024 * 1024 * 1024
    request_timeout_seconds: int = 30 * 60
    chunk_idle_timeout_seconds: int = 30
    stale_incoming_seconds: int = 24 * 60 * 60


def _load_tokens(value: str) -> tuple[TokenConfig, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise ValueError("DTCC_UPLOAD_TOKENS_JSON must be a JSON list")

    tokens: list[TokenConfig] = []
    for item in raw:
        tokens.append(
            TokenConfig(
                token_id=str(item["token_id"]),
                principal_id=str(item["principal_id"]),
                token=str(item["token"]),
                scopes=tuple(str(scope) for scope in item["scopes"]),
            )
        )
    return tuple(tokens)


def _load_aliases(value: str | None) -> dict[str, str]:
    if not value:
        return {}

    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("DTCC_UPLOAD_PRINCIPAL_ALIASES_JSON must be a JSON object")
    return {str(key): str(alias) for key, alias in raw.items()}


def _load_cors_origins(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()

    raw = json.loads(value)
    if not isinstance(raw, list):
        raise ValueError("DTCC_UPLOAD_CORS_ORIGINS_JSON must be a JSON list")
    return tuple(str(origin) for origin in raw)


def load_settings() -> Settings:
    tokens_json = os.environ.get("DTCC_UPLOAD_TOKENS_JSON", "[]")
    return Settings(
        storage_root=Path(os.environ.get("DTCC_UPLOAD_STORAGE_ROOT", "storage")),
        db_path=Path(os.environ.get("DTCC_UPLOAD_DB_PATH", "storage/catalog.sqlite3")),
        tokens=_load_tokens(tokens_json),
        principal_aliases=_load_aliases(os.environ.get("DTCC_UPLOAD_PRINCIPAL_ALIASES_JSON")),
        cors_origins=_load_cors_origins(os.environ.get("DTCC_UPLOAD_CORS_ORIGINS_JSON")),
        bind_host=os.environ.get("DTCC_UPLOAD_BIND_HOST", "127.0.0.1"),
    )
