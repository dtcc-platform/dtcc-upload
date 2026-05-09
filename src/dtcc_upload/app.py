from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import BinaryIO

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

from dtcc_upload.auth import Principal, require_principal, require_upload
from dtcc_upload.catalog import (
    Catalog,
    DatasetKeyConflict,
    DuplicateVersionContent,
    IdempotencyConflict,
    PrincipalQuotaExceeded,
)
from dtcc_upload.config import load_settings
from dtcc_upload.hash_utils import file_set_sha256, sha256_bytes
from dtcc_upload.limits import enforce_upload_bytes, enforce_upload_counts
from dtcc_upload.responses import bytes_response, file_response
from dtcc_upload.storage import Storage
from dtcc_upload.validation import extract_manifest_file_paths, validate_dataset_key, validate_manifest


class RetractionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


def create_app() -> FastAPI:
    settings = load_settings()
    catalog = Catalog(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.catalog = catalog
        catalog.init_schema()
        storage = Storage(settings.storage_root)
        storage.recover_pending(
            catalog,
            max_stored_bytes_per_principal=settings.max_stored_bytes_per_principal,
        )
        storage.cleanup_stale_incoming(catalog, settings.stale_incoming_seconds)
        storage.cleanup_stale_pending_finals(catalog, settings.stale_incoming_seconds)
        catalog.reconcile_idempotency_keys(max_age_seconds=settings.stale_incoming_seconds)
        yield

    app = FastAPI(title="DTCC Upload", lifespan=lifespan)
    app.state.settings = settings
    app.state.catalog = catalog

    @app.middleware("http")
    async def reject_large_uploads(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/v1/datasets":
            if request.headers.get("transfer-encoding", "").lower() == "chunked":
                return JSONResponse(status_code=411, content={"detail": "Content-Length required"})

            content_length = request.headers.get("content-length")
            try:
                request_bytes = int(content_length) if content_length is not None else None
            except ValueError:
                request_bytes = None

            if request_bytes is not None and request_bytes > app.state.settings.max_total_upload_bytes:
                return JSONResponse(status_code=413, content={"detail": "Upload too large"})

        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    def me(principal: Principal = Depends(require_principal)) -> dict[str, object]:
        return {
            "principal_id": principal.principal_id,
            "token_id": principal.token_id,
            "scopes": list(principal.scopes),
        }

    @app.get("/v1/datasets")
    def list_datasets(
        request: Request,
        owner: str | None = None,
        format: str | None = None,
        data_kind: str | None = None,
        product: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        include_retracted: bool = False,
        limit: int = 100,
        cursor: str | None = None,
        principal: Principal = Depends(require_principal),
    ) -> dict[str, object]:
        if not principal.has_scope("browse"):
            raise HTTPException(status_code=403, detail="Browse scope required")
        catalog = request.app.state.catalog
        try:
            return catalog.list_datasets(
                principal_id=principal.principal_id,
                include_retracted=include_retracted and principal.has_scope("upload"),
                owner=owner,
                format=format,
                data_kind=data_kind,
                product=product,
                status=status_filter,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def can_read_version(
        dataset: dict[str, object] | None,
        version: dict[str, object] | None,
        principal: Principal,
    ) -> bool:
        if dataset is None or version is None:
            return False
        if version["status"] == "committed":
            return principal.has_scope("browse")
        return (
            version["status"] == "retracted"
            and dataset["owner_principal_id"] == principal.principal_id
            and principal.has_scope("upload")
        )

    def reject_committed_without_browse(version: dict[str, object] | None, principal: Principal) -> None:
        if version is not None and version["status"] == "committed" and not principal.has_scope("browse"):
            raise HTTPException(status_code=403, detail="Browse scope required")

    @app.get("/v1/datasets/{dataset_key}")
    def get_dataset_detail(
        request: Request,
        dataset_key: str,
        include_retracted: bool = False,
        principal: Principal = Depends(require_principal),
    ) -> dict[str, object]:
        catalog = request.app.state.catalog
        dataset = catalog.get_dataset(dataset_key)
        if dataset is None:
            raise HTTPException(status_code=404, detail="Not found")

        latest = None
        latest_version_id = dataset["latest_committed_version_id"]
        if latest_version_id is not None:
            latest = catalog.get_version(dataset_key, latest_version_id)
            reject_committed_without_browse(latest, principal)
        elif not (
            include_retracted
            and dataset["owner_principal_id"] == principal.principal_id
            and principal.has_scope("upload")
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return {"dataset": dataset, "latest": latest}

    @app.get("/v1/datasets/{dataset_key}/versions")
    def list_dataset_versions(
        request: Request,
        dataset_key: str,
        include_retracted: bool = False,
        principal: Principal = Depends(require_principal),
    ) -> dict[str, object]:
        catalog = request.app.state.catalog
        dataset = catalog.get_dataset(dataset_key)
        if dataset is None:
            raise HTTPException(status_code=404, detail="Not found")

        rows = [
            version
            for version in catalog.list_versions(dataset_key)
            if (version["status"] == "committed" and principal.has_scope("browse"))
            or (
                include_retracted
                and version["status"] == "retracted"
                and dataset["owner_principal_id"] == principal.principal_id
                and principal.has_scope("upload")
            )
        ]
        if not rows and not principal.has_scope("browse"):
            raise HTTPException(status_code=403, detail="Browse scope required")
        if not rows:
            raise HTTPException(status_code=404, detail="Not found")
        return {"items": rows}

    @app.get("/v1/datasets/{dataset_key}/versions/{version_id}")
    def get_version_detail(
        request: Request,
        dataset_key: str,
        version_id: str,
        principal: Principal = Depends(require_principal),
    ) -> dict[str, object]:
        catalog = request.app.state.catalog
        dataset = catalog.get_dataset(dataset_key)
        version = catalog.get_version(dataset_key, version_id)
        reject_committed_without_browse(version, principal)
        if not can_read_version(dataset, version, principal):
            raise HTTPException(status_code=404, detail="Not found")
        return {"version": version, "files": catalog.list_files(dataset_key, version_id)}

    @app.api_route("/v1/datasets/{dataset_key}/versions/{version_id}/manifest", methods=["GET", "HEAD"])
    def download_manifest(
        request: Request,
        dataset_key: str,
        version_id: str,
        range_header: str | None = Header(default=None, alias="Range"),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        principal: Principal = Depends(require_principal),
    ):
        settings = request.app.state.settings
        catalog = request.app.state.catalog
        dataset = catalog.get_dataset(dataset_key)
        version = catalog.get_version(dataset_key, version_id)
        reject_committed_without_browse(version, principal)
        if not can_read_version(dataset, version, principal):
            raise HTTPException(status_code=404, detail="Not found")

        path = Storage(settings.storage_root).version_dir(dataset_key, version_id) / "manifest.json"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return bytes_response(
            payload,
            media_type="application/json",
            sha256=str(version["manifest_sha256"]),
            disposition="inline",
            filename="manifest.json",
            range_header=range_header,
            if_none_match=if_none_match,
        )

    @app.api_route("/v1/datasets/{dataset_key}/versions/{version_id}/files/{path:path}", methods=["GET", "HEAD"])
    def download_file(
        request: Request,
        dataset_key: str,
        version_id: str,
        path: str,
        range_header: str | None = Header(default=None, alias="Range"),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        principal: Principal = Depends(require_principal),
    ):
        settings = request.app.state.settings
        catalog = request.app.state.catalog
        dataset = catalog.get_dataset(dataset_key)
        version = catalog.get_version(dataset_key, version_id)
        reject_committed_without_browse(version, principal)
        if not can_read_version(dataset, version, principal):
            raise HTTPException(status_code=404, detail="Not found")

        files = {row["path"]: row for row in catalog.list_files(dataset_key, version_id)}
        record = files.get(path)
        if record is None:
            raise HTTPException(status_code=404, detail="Not found")

        storage = Storage(settings.storage_root)
        try:
            file_path = storage.resolve_version_file(storage.version_dir(dataset_key, version_id), path)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return file_response(
            file_path,
            media_type=str(record["sniffed_media_type"]),
            sha256=str(record["sha256"]),
            disposition="attachment",
            filename=path,
            range_header=range_header,
            if_none_match=if_none_match,
        )

    @app.post("/v1/datasets/{dataset_key}/versions/{version_id}/retract")
    def retract_version(
        request: Request,
        dataset_key: str,
        version_id: str,
        body: RetractionRequest | None = Body(default=None),
        principal: Principal = Depends(require_upload),
    ) -> dict[str, object]:
        catalog = request.app.state.catalog
        dataset = catalog.get_dataset(dataset_key)
        if dataset is None or dataset["owner_principal_id"] != principal.principal_id:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            version = catalog.retract_version(dataset_key, version_id, principal.principal_id, body.reason if body else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return {"dataset_key": dataset_key, "version_id": version_id, "status": version["status"]}

    def iter_limited_file_chunks(
        fileobj: BinaryIO,
        *,
        settings,
        chunk_size: int = 1024 * 1024,
    ):
        fileobj.seek(0)
        seen = 0
        while True:
            chunk = fileobj.read(chunk_size)
            if not chunk:
                break
            seen += len(chunk)
            enforce_upload_bytes(settings, file_bytes=seen)
            yield chunk

    def multipart_counts(request: Request, *, default_total_parts: int) -> tuple[int, int]:
        form = getattr(request, "_form", None)
        if form is None:
            return default_total_parts, 1
        items = form.multi_items()
        non_file_fields = sum(1 for _, value in items if not isinstance(value, StarletteUploadFile))
        return len(items), non_file_fields

    def version_response(
        *,
        catalog: Catalog,
        dataset_key: str,
        version: dict[str, object],
        owner: str,
    ) -> dict[str, object]:
        return {
            "dataset_key": dataset_key,
            "version_id": version["version_id"],
            "version_number": version["version_number"],
            "owner": owner,
            "status": version["status"],
            "manifest_sha256": version["manifest_sha256"],
            "file_set_sha256": version["file_set_sha256"],
            "files": catalog.list_files(dataset_key, str(version["version_id"])),
        }

    @app.post("/v1/datasets")
    def upload_dataset(
        request: Request,
        dataset_key: str = Form(...),
        manifest: UploadFile = File(...),
        files: list[UploadFile] = File(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal: Principal = Depends(require_upload),
    ) -> dict[str, object]:
        settings = request.app.state.settings
        try:
            dataset_key = validate_dataset_key(dataset_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        total_parts, non_file_fields = multipart_counts(request, default_total_parts=len(files) + 2)
        enforce_upload_counts(
            settings,
            file_count=len(files),
            total_parts=total_parts,
            non_file_fields=non_file_fields,
        )

        manifest.file.seek(0)
        manifest_bytes = manifest.file.read()
        if len(manifest_bytes) > settings.max_manifest_bytes:
            raise HTTPException(status_code=413, detail="Manifest too large")

        try:
            raw_manifest = json.loads(manifest_bytes)
            parsed_manifest = validate_manifest(raw_manifest)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid manifest: {exc}") from exc

        expected_paths = extract_manifest_file_paths(parsed_manifest)
        if len(expected_paths) != 1 or len(files) != 1:
            raise HTTPException(status_code=400, detail="Uploaded files do not match manifest")

        catalog = getattr(request.app.state, "catalog", None) or Catalog(settings.db_path)
        try:
            catalog.claim_dataset(dataset_key, principal.principal_id)
        except DatasetKeyConflict as exc:
            raise HTTPException(status_code=409, detail="dataset_key is owned by another principal") from exc

        logical_path = expected_paths[0]
        upload = files[0]
        version_id = uuid.uuid4().hex
        upload_id = uuid.uuid4().hex
        request_id = uuid.uuid4().hex
        storage = Storage(settings.storage_root)
        committed = False
        pending_inserted = False
        finalized = False
        reserved_idempotency = False
        request_hash = ""

        try:
            staged = storage.create_staging_dir(upload_id)
            storage.write_manifest(staged, manifest_bytes)

            file_info = storage.write_staged_file(
                staged,
                logical_path,
                iter_limited_file_chunks(upload.file, settings=settings),
            )
            total_bytes = int(file_info["size"])
            enforce_upload_bytes(settings, total_bytes=total_bytes)
            file_records = [
                {
                    "path": logical_path,
                    "original_filename": upload.filename,
                    "size": total_bytes,
                    "sha256": str(file_info["sha256"]),
                    "media_type": parsed_manifest.media_type,
                    "sniffed_media_type": parsed_manifest.media_type,
                }
            ]

            manifest_sha = sha256_bytes(manifest_bytes)
            file_set_sha = file_set_sha256(file_records)
            request_hash = sha256_bytes(
                json.dumps(
                    {
                        "dataset_key": dataset_key,
                        "manifest_sha256": manifest_sha,
                        "file_set_sha256": file_set_sha,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            if idempotency_key is not None:
                try:
                    reservation = catalog.reserve_idempotency_key(
                        principal.principal_id,
                        dataset_key,
                        idempotency_key,
                        request_hash,
                    )
                except IdempotencyConflict as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency-Key conflicts with previous request",
                    ) from exc
                reserved_idempotency = bool(reservation["_created"])
                if not reserved_idempotency:
                    if reservation["status"] == "committed":
                        return json.loads(str(reservation["response_json"]))
                    raise HTTPException(status_code=409, detail="Idempotency-Key is already in progress")

            duplicate = catalog.find_committed_duplicate(dataset_key, manifest_sha, file_set_sha)
            if duplicate is not None:
                response_payload = version_response(
                    catalog=catalog,
                    dataset_key=dataset_key,
                    version=duplicate,
                    owner=principal.principal_id,
                )
                if idempotency_key is not None:
                    catalog.complete_idempotency_key(
                        principal.principal_id,
                        dataset_key,
                        idempotency_key,
                        request_hash,
                        str(duplicate["version_id"]),
                        json.dumps(response_payload),
                    )
                    reserved_idempotency = False
                return response_payload

            try:
                pending = catalog.insert_pending_version(
                    dataset_key=dataset_key,
                    version_id=version_id,
                    principal_id=principal.principal_id,
                    manifest_sha256=manifest_sha,
                    file_set_sha256=file_set_sha,
                    manifest_size=len(manifest_bytes),
                    format=parsed_manifest.format,
                    media_type=parsed_manifest.media_type,
                    data_kind=parsed_manifest.data_kind,
                    product=parsed_manifest.product,
                    title=parsed_manifest.title,
                    bounds_json=json.dumps(parsed_manifest.bounds) if parsed_manifest.bounds is not None else None,
                    total_bytes=total_bytes,
                    file_count=len(file_records),
                    request_id=request_id,
                    upload_id=upload_id,
                )
                pending_inserted = True
            except DuplicateVersionContent as exc:
                duplicate = catalog.find_committed_duplicate(dataset_key, manifest_sha, file_set_sha)
                if duplicate is None:
                    raise HTTPException(status_code=409, detail="Duplicate upload is still pending") from exc
                response_payload = version_response(
                    catalog=catalog,
                    dataset_key=dataset_key,
                    version=duplicate,
                    owner=principal.principal_id,
                )
                if idempotency_key is not None:
                    catalog.complete_idempotency_key(
                        principal.principal_id,
                        dataset_key,
                        idempotency_key,
                        request_hash,
                        str(duplicate["version_id"]),
                        json.dumps(response_payload),
                    )
                    reserved_idempotency = False
                return response_payload

            for record in file_records:
                catalog.insert_file_record(dataset_key=dataset_key, version_id=version_id, **record)

            storage.write_commit_ready(
                staged,
                {
                    "dataset_key": dataset_key,
                    "version_id": version_id,
                    "manifest_sha256": manifest_sha,
                    "file_set_sha256": file_set_sha,
                },
            )
            storage.finalize(staged, dataset_key, version_id)
            finalized = True
            try:
                catalog.commit_version(
                    dataset_key,
                    version_id,
                    max_stored_bytes_per_principal=settings.max_stored_bytes_per_principal,
                )
            except PrincipalQuotaExceeded as exc:
                raise HTTPException(status_code=413, detail="Principal storage quota exceeded") from exc
            committed = True
            committed_version = catalog.get_version(dataset_key, version_id) or pending
            response_payload = version_response(
                catalog=catalog,
                dataset_key=dataset_key,
                version=committed_version,
                owner=principal.principal_id,
            )
            if idempotency_key is not None:
                catalog.complete_idempotency_key(
                    principal.principal_id,
                    dataset_key,
                    idempotency_key,
                    request_hash,
                    version_id,
                    json.dumps(response_payload),
                )
                reserved_idempotency = False
            return response_payload
        finally:
            if not committed:
                if finalized:
                    storage.cleanup_version_dir(dataset_key, version_id)
                if pending_inserted:
                    catalog.delete_pending_version(dataset_key, version_id)
                if reserved_idempotency and idempotency_key is not None:
                    catalog.delete_pending_idempotency_key(
                        principal.principal_id,
                        dataset_key,
                        idempotency_key,
                        request_hash,
                    )
                storage.cleanup_staging(upload_id)

    return app
