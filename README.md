# DTCC Upload

Dataset Manifest v3 (`dtcc-dataset-manifest-v3`) uses the same multipart and
retrieval endpoints. It requires exactly one `canonical_model` artifact, explicit
model type/wire version, and size/SHA-256 declarations for every artifact.
Derivatives reference the canonical artifact. The service preserves the original
manifest/context and model bytes and verifies upload integrity; semantic model
validation remains the producer/consumer's responsibility in Core. No Core or
LinkML runtime dependency is required here.


Small FastAPI backend for accepting precomputed DTCC dataset manifests and attached files.

This v1 storage implementation targets POSIX filesystems. Crash-durability paths use `fsync` on files and parent directories.
Retracted versions are retained for audit and continue counting toward principal storage quota until an operator deletes them out of band.

## Dataset Uploads

The `/v1/datasets` endpoint remains the compatibility API. It accepts the existing v1 single-file manifest shape with `manifest.file`, and v1 uploads must still include exactly one multipart `files` part.

Dataset Manifest v2 packages are also accepted when `schema_version` is `dtcc-dataset-manifest-v2`. V2 manifests use `artifacts[]`; clients upload `manifest.json` plus one multipart `files` part for each artifact. Each uploaded file name must match either the artifact path from `manifest.artifacts[].path` or that path's basename. Basename matching is allowed only when the basename identifies exactly one artifact; packages with duplicate artifact basenames should upload using exact artifact paths.

For object-first packages produced by dtcc-core, submit the unpacked package contents as multipart data, for example:

```text
manifest.json
artifacts/smoke_slice.png
artifacts/smoke_slice.geojson
```

The uploaded manifest is stored unchanged and remains the source of truth. Version catalog summary fields for v2 are derived from the primary artifact when possible.

Consumers retrieve committed packages through the version endpoints:

```text
GET /v1/datasets/{dataset_key}/versions/{version_id}
GET /v1/datasets/{dataset_key}/versions/{version_id}/manifest
GET /v1/datasets/{dataset_key}/versions/{version_id}/files/{artifact_path}
```

The file route accepts nested v2 artifact paths such as
`artifacts/smoke_slice.png`. Atlas and tangible-twin should read
`manifest.artifacts[]` from the manifest response and select displayable
artifacts by role, media type, and data kind rather than guessing file names.

## Local Development

```bash
uv run --extra test pytest
uv run uvicorn dtcc_upload.app:create_app --factory --host 127.0.0.1 --port 8000
```

## Token Configuration

Set `DTCC_UPLOAD_TOKENS_JSON` to a JSON array:

```json
[
  {"token_id": "logg-dev", "principal_id": "logg", "token": "replace-me", "scopes": ["upload", "browse"]},
  {"token_id": "vasnas-dev", "principal_id": "vasnas", "token": "replace-me-too", "scopes": ["upload", "browse"]},
  {"token_id": "browser-dev", "principal_id": "browser", "token": "replace-me-browser", "scopes": ["browse"]}
]
```

If a token uses an alternate principal name, set `DTCC_UPLOAD_PRINCIPAL_ALIASES_JSON`, for example:

```json
{"vbassn": "vasnas"}
```

To allow a browser app on another origin to browse the catalog, set `DTCC_UPLOAD_CORS_ORIGINS_JSON`:

```json
["http://localhost:5175"]
```

Uploads run in Starlette's worker threadpool because the route is a sync `def`.
For non-trivial deployments, use multiple Uvicorn workers and tune the AnyIO
thread limiter if large concurrent uploads saturate the default pool.
