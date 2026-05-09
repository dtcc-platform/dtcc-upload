from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from dtcc_upload.config import Settings, TokenConfig


@dataclass(frozen=True)
class Principal:
    principal_id: str
    token_id: str
    scopes: tuple[str, ...]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def token_prefix_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _extract_bearer(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")

    token = token.strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
    return token


def _record_auth_failure(request: Request, reason: str) -> None:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        return
    try:
        catalog.record_event(
            action="auth_failure",
            reason=reason,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        return


def resolve_principal(token: str, settings: Settings) -> Principal | None:
    matched: TokenConfig | None = None
    for candidate in settings.tokens:
        if hmac.compare_digest(token, candidate.token):
            matched = candidate

    if matched is None:
        return None

    principal_id = settings.principal_aliases.get(matched.principal_id, matched.principal_id)
    return Principal(
        principal_id=principal_id,
        token_id=matched.token_id,
        scopes=matched.scopes,
    )


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    try:
        token = _extract_bearer(authorization)
    except HTTPException as exc:
        _record_auth_failure(request, str(exc.detail))
        raise
    principal = resolve_principal(token, settings)
    if principal is None:
        _record_auth_failure(request, "Invalid bearer token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
    return principal


def require_upload(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    if not principal.has_scope("upload"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload scope required")
    return principal
