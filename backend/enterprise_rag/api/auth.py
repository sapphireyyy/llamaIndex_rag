"""Authentication dependencies separated from persisted tenant authorization."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, cast

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_context import (
    require_platform_permission,
    resolve_tenant_context,
)
from enterprise_rag.config import Settings
from enterprise_rag.domain.types import (
    AuthenticatedSubject,
    ErrorCategory,
    PlatformRole,
    RequestIdentity,
)
from enterprise_rag.infrastructure.database import bind_request_tenant

bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=8)
def jwk_client(url: str) -> PyJWKClient:
    """Cache the OIDC JSON Web Key client without caching authorization state."""
    return PyJWKClient(url, cache_keys=True)


def _platform_roles_from_claims(claims: dict[str, Any]) -> frozenset[PlatformRole]:
    """Read only explicit trusted platform roles from verified token claims."""
    raw_roles = set(claims.get("roles", []))
    realm_access = claims.get("realm_access", {})
    if isinstance(realm_access, dict):
        raw_roles.update(realm_access.get("roles", []))
    roles: set[PlatformRole] = set()
    for value in raw_roles:
        try:
            roles.add(PlatformRole(str(value)))
        except ValueError:
            continue
    return frozenset(roles)


def verify_oidc_token(token: str, settings: Settings) -> AuthenticatedSubject:
    """Verify login claims and return a tenant-neutral immutable subject."""
    try:
        signing_key = jwk_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        return AuthenticatedSubject(
            subject_id=str(claims["sub"]),
            groups=tuple(str(group) for group in claims.get("groups", [])),
            platform_roles=_platform_roles_from_claims(claims),
            tenant_hint=str(claims["tenant_id"]) if claims.get("tenant_id") else None,
            token_id=str(claims["jti"]) if claims.get("jti") else None,
            claims=claims,
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AppError(ErrorCategory.UNAUTHENTICATED, "Authentication failed.", 401) from exc


async def get_subject(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedSubject:
    """Verify one login subject without accepting any tenant authorization claims."""
    settings: Settings = request.app.state.settings
    if settings.oidc_enabled:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AppError(ErrorCategory.UNAUTHENTICATED, "Authentication required.", 401)
        subject = verify_oidc_token(credentials.credentials, settings)
    elif settings.dev_auth_enabled and settings.environment in {"development", "test"}:
        subject = AuthenticatedSubject(
            subject_id=settings.dev_user_id,
            groups=tuple(settings.dev_groups),
            platform_roles=frozenset({PlatformRole.PLATFORM_ADMINISTRATOR})
            if settings.dev_platform_admin
            else frozenset(),
            tenant_hint=settings.dev_tenant_id,
        )
    else:
        raise AppError(ErrorCategory.UNAUTHENTICATED, "Authentication required.", 401)
    request.state.subject = subject
    return subject


async def get_identity(
    request: Request,
    subject: Annotated[AuthenticatedSubject, Depends(get_subject)],
    selected_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> RequestIdentity:
    """Resolve the selected tenant from live principal, membership and group records."""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(
            ErrorCategory.DEPENDENCY_UNAVAILABLE,
            "Tenant authorization is unavailable.",
            503,
        )
    factory = cast(async_sessionmaker[AsyncSession], sessions)
    try:
        async with factory() as session:
            identity = await resolve_tenant_context(session, subject, selected_tenant_id)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCategory.DEPENDENCY_UNAVAILABLE,
            "Tenant authorization is unavailable.",
            503,
        ) from exc
    request.state.identity = identity
    request.state.tenant_id = identity.tenant_id
    bind_request_tenant(identity.tenant_id)
    return identity


async def get_platform_subject(
    subject: Annotated[AuthenticatedSubject, Depends(get_subject)],
) -> AuthenticatedSubject:
    """Require the trusted platform administrator role for control-plane endpoints."""
    require_platform_permission(subject, "platform.tenant.read")
    return subject


Subject = Annotated[AuthenticatedSubject, Depends(get_subject)]
PlatformSubject = Annotated[AuthenticatedSubject, Depends(get_platform_subject)]
Identity = Annotated[RequestIdentity, Depends(get_identity)]
