from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DatasetKeyConflict(RuntimeError):
    pass


class DuplicateVersionContent(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class PrincipalQuotaExceeded(RuntimeError):
    pass


class Catalog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS principals (
                  principal_id TEXT PRIMARY KEY,
                  display_name TEXT NOT NULL,
                  scopes_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS datasets (
                  dataset_key TEXT PRIMARY KEY,
                  owner_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
                  version_counter INTEGER NOT NULL DEFAULT 0,
                  latest_committed_version_id TEXT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS versions (
                  dataset_key TEXT NOT NULL REFERENCES datasets(dataset_key),
                  version_id TEXT NOT NULL,
                  version_number INTEGER NOT NULL,
                  uploaded_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
                  status TEXT NOT NULL CHECK (status IN ('pending','committed','retracted')),
                  manifest_sha256 TEXT NOT NULL,
                  file_set_sha256 TEXT NOT NULL,
                  manifest_size INTEGER NOT NULL,
                  format TEXT NOT NULL,
                  media_type TEXT NOT NULL,
                  data_kind TEXT NOT NULL,
                  product TEXT NULL,
                  title TEXT NULL,
                  bounds_json TEXT NULL,
                  total_bytes INTEGER NOT NULL,
                  file_count INTEGER NOT NULL,
                  request_id TEXT NOT NULL,
                  upload_id TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  committed_at TEXT NULL,
                  retracted_at TEXT NULL,
                  retracted_by_principal_id TEXT NULL REFERENCES principals(principal_id),
                  retraction_reason TEXT NULL,
                  PRIMARY KEY (dataset_key, version_id),
                  UNIQUE (dataset_key, version_number),
                  UNIQUE (dataset_key, manifest_sha256, file_set_sha256)
                );
                CREATE TABLE IF NOT EXISTS files (
                  dataset_key TEXT NOT NULL,
                  version_id TEXT NOT NULL,
                  path TEXT NOT NULL,
                  original_filename TEXT NULL,
                  size INTEGER NOT NULL,
                  sha256 TEXT NOT NULL,
                  media_type TEXT NOT NULL,
                  sniffed_media_type TEXT NOT NULL,
                  PRIMARY KEY (dataset_key, version_id, path),
                  FOREIGN KEY (dataset_key, version_id) REFERENCES versions(dataset_key, version_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  actor_principal_id TEXT NULL REFERENCES principals(principal_id),
                  action TEXT NOT NULL,
                  dataset_key TEXT NULL,
                  version_id TEXT NULL,
                  request_id TEXT NULL,
                  reason TEXT NULL,
                  ip TEXT NULL,
                  user_agent TEXT NULL,
                  token_id TEXT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                  principal_id TEXT NOT NULL,
                  dataset_key TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  -- Redundant in v1; kept as response metadata for future cross-key result shapes.
                  dataset_key_result TEXT NOT NULL,
                  version_id_result TEXT NULL,
                  response_json TEXT NULL,
                  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','committed')),
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (principal_id, dataset_key, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS principal_usage (
                  principal_id TEXT PRIMARY KEY REFERENCES principals(principal_id),
                  stored_bytes INTEGER NOT NULL DEFAULT 0,
                  version_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_versions_listing
                  ON versions(dataset_key, status, committed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_versions_committed_at
                  ON versions(committed_at DESC, dataset_key);
                CREATE INDEX IF NOT EXISTS idx_versions_stale
                  ON versions(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_versions_format
                  ON versions(format);
                CREATE INDEX IF NOT EXISTS idx_versions_data_kind
                  ON versions(data_kind);
                CREATE INDEX IF NOT EXISTS idx_versions_product
                  ON versions(product);
                CREATE INDEX IF NOT EXISTS idx_files_sha256
                  ON files(sha256);
                """
            )
            for principal_id, display_name, scopes in [
                ("logg", "logg", ["upload", "browse"]),
                ("vasnas", "vasnas", ["upload", "browse"]),
                ("browser", "browser", ["browse"]),
            ]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO principals(
                      principal_id,
                      display_name,
                      scopes_json
                    )
                    VALUES (?, ?, ?)
                    """,
                    (principal_id, display_name, json.dumps(scopes)),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO principal_usage(principal_id) VALUES (?)",
                    (principal_id,),
                )

    def list_principals(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT * FROM principals ORDER BY principal_id")
            ]

    def claim_dataset(self, dataset_key: str, owner_principal_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM datasets WHERE dataset_key = ?",
                (dataset_key,),
            ).fetchone()
            if existing is not None:
                if existing["owner_principal_id"] != owner_principal_id:
                    raise DatasetKeyConflict(dataset_key)
                return dict(existing)

            try:
                conn.execute(
                    """
                    INSERT INTO datasets(dataset_key, owner_principal_id)
                    VALUES (?, ?)
                    """,
                    (dataset_key, owner_principal_id),
                )
            except sqlite3.IntegrityError as exc:
                existing = conn.execute(
                    "SELECT * FROM datasets WHERE dataset_key = ?",
                    (dataset_key,),
                ).fetchone()
                if existing is not None and existing["owner_principal_id"] == owner_principal_id:
                    return dict(existing)
                raise DatasetKeyConflict(dataset_key) from exc

            row = conn.execute(
                "SELECT * FROM datasets WHERE dataset_key = ?",
                (dataset_key,),
            ).fetchone()
            return dict(row)

    def allocate_version_number(self, dataset_key: str) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE datasets
                SET version_counter = version_counter + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE dataset_key = ?
                RETURNING version_counter
                """,
                (dataset_key,),
            ).fetchone()
            if row is None:
                raise KeyError(dataset_key)
            return int(row["version_counter"])

    def insert_pending_version(self, **values: Any) -> dict[str, Any]:
        if "principal_id" in values:
            values["uploaded_by_principal_id"] = values.pop("principal_id")
        values["status"] = "pending"

        with self.connection() as conn:
            if "version_number" not in values:
                row = conn.execute(
                    """
                    UPDATE datasets
                    SET version_counter = version_counter + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE dataset_key = ?
                    RETURNING version_counter
                    """,
                    (values["dataset_key"],),
                ).fetchone()
                if row is None:
                    raise KeyError(values["dataset_key"])
                values["version_number"] = int(row["version_counter"])
            else:
                conn.execute(
                    """
                    UPDATE datasets
                    SET version_counter = max(version_counter, ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE dataset_key = ?
                    """,
                    (int(values["version_number"]), values["dataset_key"]),
                )

            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            try:
                conn.execute(
                    f"INSERT INTO versions({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc)
                if "manifest_sha256" in message or "file_set_sha256" in message:
                    raise DuplicateVersionContent("Duplicate version content") from exc
                raise

        version = self.get_version(values["dataset_key"], values["version_id"])
        assert version is not None
        return version

    def commit_version(
        self,
        dataset_key: str,
        version_id: str,
        *,
        max_stored_bytes_per_principal: int | None = None,
    ) -> None:
        with self.connection() as conn:
            version = conn.execute(
                """
                SELECT uploaded_by_principal_id, total_bytes
                FROM versions
                WHERE dataset_key = ? AND version_id = ? AND status = 'pending'
                """,
                (dataset_key, version_id),
            ).fetchone()
            if version is None:
                raise RuntimeError("Version is not pending")

            if max_stored_bytes_per_principal is not None:
                usage_cursor = conn.execute(
                    """
                    UPDATE principal_usage
                    SET stored_bytes = stored_bytes + ?,
                        version_count = version_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = ?
                      AND stored_bytes + ? <= ?
                    """,
                    (
                        int(version["total_bytes"]),
                        version["uploaded_by_principal_id"],
                        int(version["total_bytes"]),
                        max_stored_bytes_per_principal,
                    ),
                )
                if usage_cursor.rowcount != 1:
                    raise PrincipalQuotaExceeded("Principal storage quota exceeded")
            else:
                conn.execute(
                    """
                    UPDATE principal_usage
                    SET stored_bytes = stored_bytes + ?,
                        version_count = version_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE principal_id = ?
                    """,
                    (int(version["total_bytes"]), version["uploaded_by_principal_id"]),
                )

            cursor = conn.execute(
                """
                UPDATE versions
                SET status = 'committed',
                    committed_at = CURRENT_TIMESTAMP
                WHERE dataset_key = ? AND version_id = ? AND status = 'pending'
                """,
                (dataset_key, version_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Version is not pending")

            conn.execute(
                """
                UPDATE datasets
                SET latest_committed_version_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE dataset_key = ?
                """,
                (version_id, dataset_key),
            )

    def get_dataset(self, dataset_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE dataset_key = ?",
                (dataset_key,),
            ).fetchone()
            return dict(row) if row is not None else None

    def get_version(self, dataset_key: str, version_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM versions
                WHERE dataset_key = ? AND version_id = ?
                """,
                (dataset_key, version_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_pending_versions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM versions
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    """
                )
            ]

    def delete_pending_version(self, dataset_key: str, version_id: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM files WHERE dataset_key = ? AND version_id = ?",
                (dataset_key, version_id),
            )
            conn.execute(
                "DELETE FROM versions WHERE dataset_key = ? AND version_id = ? AND status = 'pending'",
                (dataset_key, version_id),
            )

    def insert_file_record(
        self,
        *,
        dataset_key: str,
        version_id: str,
        path: str,
        original_filename: str | None,
        size: int,
        sha256: str,
        media_type: str,
        sniffed_media_type: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO files(
                  dataset_key, version_id, path, original_filename,
                  size, sha256, media_type, sniffed_media_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (dataset_key, version_id, path, original_filename, size, sha256, media_type, sniffed_media_type),
            )

    def list_files(self, dataset_key: str, version_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM files WHERE dataset_key = ? AND version_id = ? ORDER BY path",
                    (dataset_key, version_id),
                )
            ]

    def list_versions(self, dataset_key: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM versions
                    WHERE dataset_key = ? AND status IN ('committed', 'retracted')
                    ORDER BY version_number DESC
                    """,
                    (dataset_key,),
                )
            ]

    def retract_version(
        self,
        dataset_key: str,
        version_id: str,
        principal_id: str,
        reason: str | None,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            version = conn.execute(
                "SELECT * FROM versions WHERE dataset_key = ? AND version_id = ?",
                (dataset_key, version_id),
            ).fetchone()
            if version is None:
                raise KeyError(version_id)
            if version["status"] == "retracted":
                return dict(version)

            # Retraction is audit retention, not deletion; principal_usage intentionally stays consumed.
            conn.execute(
                """
                UPDATE versions
                SET status = 'retracted',
                    retracted_at = CURRENT_TIMESTAMP,
                    retracted_by_principal_id = ?,
                    retraction_reason = ?
                WHERE dataset_key = ? AND version_id = ?
                """,
                (principal_id, reason, dataset_key, version_id),
            )
            latest = conn.execute(
                """
                SELECT version_id
                FROM versions
                WHERE dataset_key = ? AND status = 'committed'
                ORDER BY version_number DESC
                LIMIT 1
                """,
                (dataset_key,),
            ).fetchone()
            conn.execute(
                """
                UPDATE datasets
                SET latest_committed_version_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE dataset_key = ?
                """,
                (latest["version_id"] if latest is not None else None, dataset_key),
            )

        result = self.get_version(dataset_key, version_id)
        assert result is not None
        return result

    def reserve_idempotency_key(
        self,
        principal_id: str,
        dataset_key: str,
        key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency_keys(
                      principal_id,
                      dataset_key,
                      idempotency_key,
                      request_hash,
                      dataset_key_result
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (principal_id, dataset_key, key, request_hash, dataset_key),
                )
            except sqlite3.IntegrityError as exc:
                row = conn.execute(
                    """
                    SELECT *
                    FROM idempotency_keys
                    WHERE principal_id = ? AND dataset_key = ? AND idempotency_key = ?
                    """,
                    (principal_id, dataset_key, key),
                ).fetchone()
                if row is None:
                    raise
                if row["request_hash"] != request_hash:
                    raise IdempotencyConflict(key) from exc
                result = dict(row)
                result["_created"] = False
                return result

            row = conn.execute(
                """
                SELECT *
                FROM idempotency_keys
                WHERE principal_id = ? AND dataset_key = ? AND idempotency_key = ?
                """,
                (principal_id, dataset_key, key),
            ).fetchone()
            result = dict(row)
            result["_created"] = True
            return result

    def complete_idempotency_key(
        self,
        principal_id: str,
        dataset_key: str,
        key: str,
        request_hash: str,
        version_id: str,
        response_json: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE idempotency_keys
                SET status = 'committed',
                    version_id_result = ?,
                    response_json = ?
                WHERE principal_id = ?
                  AND dataset_key = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND status = 'pending'
                """,
                (version_id, response_json, principal_id, dataset_key, key, request_hash),
            )

    def delete_pending_idempotency_key(
        self,
        principal_id: str,
        dataset_key: str,
        key: str,
        request_hash: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                DELETE FROM idempotency_keys
                WHERE principal_id = ?
                  AND dataset_key = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND status = 'pending'
                """,
                (principal_id, dataset_key, key, request_hash),
            )

    def reconcile_idempotency_keys(
        self,
        *,
        max_age_seconds: int,
        now: float | None = None,
    ) -> dict[str, int]:
        current_time = datetime.now(timezone.utc).timestamp() if now is None else now
        completed = 0
        deleted = 0
        with self.connection() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM idempotency_keys
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    """
                )
            ]
            for idem in rows:
                version = self._find_committed_version_for_request_hash(conn, idem)
                if version is not None:
                    files = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT * FROM files WHERE dataset_key = ? AND version_id = ? ORDER BY path",
                            (version["dataset_key"], version["version_id"]),
                        )
                    ]
                    response_payload = {
                        "dataset_key": version["dataset_key"],
                        "version_id": version["version_id"],
                        "version_number": version["version_number"],
                        "owner": version["uploaded_by_principal_id"],
                        "status": version["status"],
                        "manifest_sha256": version["manifest_sha256"],
                        "file_set_sha256": version["file_set_sha256"],
                        "files": files,
                    }
                    cursor = conn.execute(
                        """
                        UPDATE idempotency_keys
                        SET status = 'committed',
                            version_id_result = ?,
                            response_json = ?
                        WHERE principal_id = ?
                          AND dataset_key = ?
                          AND idempotency_key = ?
                          AND request_hash = ?
                          AND status = 'pending'
                        """,
                        (
                            version["version_id"],
                            json.dumps(response_payload),
                            idem["principal_id"],
                            idem["dataset_key"],
                            idem["idempotency_key"],
                            idem["request_hash"],
                        ),
                    )
                    completed += cursor.rowcount
                    continue

                if current_time - self._created_at_timestamp(str(idem["created_at"])) > max_age_seconds:
                    cursor = conn.execute(
                        """
                        DELETE FROM idempotency_keys
                        WHERE principal_id = ?
                          AND dataset_key = ?
                          AND idempotency_key = ?
                          AND request_hash = ?
                          AND status = 'pending'
                        """,
                        (
                            idem["principal_id"],
                            idem["dataset_key"],
                            idem["idempotency_key"],
                            idem["request_hash"],
                        ),
                    )
                    deleted += cursor.rowcount
        return {"completed": completed, "deleted": deleted}

    def _find_committed_version_for_request_hash(
        self,
        conn: sqlite3.Connection,
        idem: dict[str, Any],
    ) -> sqlite3.Row | None:
        rows = conn.execute(
            """
            SELECT *
            FROM versions
            WHERE dataset_key = ? AND status = 'committed'
            ORDER BY committed_at DESC, version_number DESC
            """,
            (idem["dataset_key"],),
        ).fetchall()
        for version in rows:
            if self._request_hash_for_version(version) == idem["request_hash"]:
                return version
        return None

    def _request_hash_for_version(self, version: sqlite3.Row) -> str:
        payload = json.dumps(
            {
                "dataset_key": version["dataset_key"],
                "manifest_sha256": version["manifest_sha256"],
                "file_set_sha256": version["file_set_sha256"],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _created_at_timestamp(self, value: str) -> float:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()

    def find_committed_duplicate(
        self,
        dataset_key: str,
        manifest_sha256: str,
        file_set_sha256: str,
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM versions
                WHERE dataset_key = ?
                  AND manifest_sha256 = ?
                  AND file_set_sha256 = ?
                  AND status = 'committed'
                """,
                (dataset_key, manifest_sha256, file_set_sha256),
            ).fetchone()
            return dict(row) if row else None

    def _encode_cursor(self, row: dict[str, Any]) -> str:
        # Cursor is intentionally opaque but unsigned; it only controls read ordering.
        payload = json.dumps(
            {
                "committed_at": row["committed_at"],
                "dataset_key": row["dataset_key"],
                "version_id": row["version_id"],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii")

    def _decode_cursor(self, cursor: str) -> dict[str, str]:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
            payload = json.loads(raw)
            return {
                "committed_at": str(payload["committed_at"]),
                "dataset_key": str(payload["dataset_key"]),
                "version_id": str(payload["version_id"]),
            }
        except Exception as exc:
            raise ValueError("Invalid cursor") from exc

    def list_datasets(
        self,
        *,
        principal_id: str,
        include_retracted: bool = False,
        owner: str | None = None,
        format: str | None = None,
        data_kind: str | None = None,
        product: str | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        visibility = ["(v.status = 'committed' AND v.version_id = d.latest_committed_version_id)"]
        params: list[Any] = []
        if include_retracted:
            visibility.append("(v.status = 'retracted' AND d.owner_principal_id = ?)")
            params.append(principal_id)
        predicates = ["(" + " OR ".join(visibility) + ")"]
        if owner is not None:
            predicates.append("d.owner_principal_id = ?")
            params.append(owner)
        if format is not None:
            predicates.append("v.format = ?")
            params.append(format)
        if data_kind is not None:
            predicates.append("v.data_kind = ?")
            params.append(data_kind)
        if product is not None:
            predicates.append("v.product = ?")
            params.append(product)
        if status is not None:
            if status == "retracted" and not include_retracted:
                return {"items": [], "next_cursor": None}
            if status not in {"committed", "retracted"}:
                raise ValueError("Unsupported status filter")
            predicates.append("v.status = ?")
            params.append(status)
        if cursor is not None:
            decoded = self._decode_cursor(cursor)
            predicates.append(
                """
                (
                  v.committed_at < ?
                  OR (v.committed_at = ? AND d.dataset_key > ?)
                  OR (v.committed_at = ? AND d.dataset_key = ? AND v.version_id > ?)
                )
                """
            )
            params.extend(
                [
                    decoded["committed_at"],
                    decoded["committed_at"],
                    decoded["dataset_key"],
                    decoded["committed_at"],
                    decoded["dataset_key"],
                    decoded["version_id"],
                ]
            )
        with self.connection() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT d.dataset_key, d.owner_principal_id, d.latest_committed_version_id,
                           v.version_id, v.version_number, v.status, v.format,
                           v.media_type, v.data_kind, v.product, v.committed_at
                    FROM datasets d
                    JOIN versions v ON v.dataset_key = d.dataset_key
                    WHERE {" AND ".join(predicates)}
                    ORDER BY v.committed_at DESC, d.dataset_key ASC, v.version_id ASC
                    LIMIT ?
                    """,
                    (*params, limit + 1),
                )
            ]
        items = rows[:limit]
        return {
            "items": items,
            "next_cursor": self._encode_cursor(items[-1]) if len(rows) > limit and items else None,
        }
