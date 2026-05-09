# DTCC Upload

Small FastAPI backend for accepting precomputed DTCC dataset manifests and attached files.

This v1 storage implementation targets POSIX filesystems. Crash-durability paths use `fsync` on files and parent directories.
Retracted versions are retained for audit and continue counting toward principal storage quota until an operator deletes them out of band.

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

Uploads run in Starlette's worker threadpool because the route is a sync `def`.
For non-trivial deployments, use multiple Uvicorn workers and tune the AnyIO
thread limiter if large concurrent uploads saturate the default pool.
