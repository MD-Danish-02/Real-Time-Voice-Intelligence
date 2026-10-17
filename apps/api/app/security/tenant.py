"""
Resolves the authenticated user and tenant from the JWT access token in
the Authorization header. tenant_id is NEVER accepted from the request
body/query/path - it always comes from here, which is the one place a
token is decoded and trusted.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token, verify_password
from app.db.session import get_async_db
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import APIKey, User

API_KEY_PREFIX_LEN = 10

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class RequestContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


async def _try_api_key_context(token: str, db: AsyncSession) -> "RequestContext | None":
    """
    API keys (`vx_...`, created via POST /api-keys) authenticate as a fixed
    'integration' role - broad enough for the automation use cases API
    keys exist for (import leads, queue calls, read analytics), but never
    user-management or billing-level actions, which require RBAC's owner
    role and therefore a real user JWT.
    """
    if not token.startswith("vx_") or len(token) < API_KEY_PREFIX_LEN:
        return None
    prefix = token[:API_KEY_PREFIX_LEN]
    result = await db.execute(select(APIKey).where(APIKey.key_prefix == prefix, APIKey.revoked.is_(False)))
    for candidate in result.scalars().all():  # prefix is not unique enough alone; verify the full key's hash
        if verify_password(token, candidate.hashed_key):
            candidate.last_used_at = datetime.now(timezone.utc)
            await db.commit()
            return RequestContext(user_id=candidate.created_by_user_id, tenant_id=candidate.tenant_id,
                                  role=UserRole.ADMIN.value)
    return None


async def get_request_context(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_async_db),
) -> RequestContext:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={
            "error": {"code": "MISSING_TOKEN", "message": "Authorization bearer token is required."}
        })
    api_key_ctx = await _try_api_key_context(credentials.credentials, db)
    if api_key_ctx is not None:
        return api_key_ctx
    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={
            "error": {"code": "INVALID_TOKEN", "message": "The provided token is invalid or expired."}
        })
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={
            "error": {"code": "WRONG_TOKEN_TYPE", "message": "An access token is required for this endpoint."}
        })
    try:
        return RequestContext(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            role=payload.get("role", "agent_viewer"),
        )
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={
            "error": {"code": "MALFORMED_TOKEN", "message": "Token payload is malformed."}
        })


async def get_current_user(
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_async_db),
) -> User:
    result = await db.execute(select(User).where(User.id == ctx.user_id, User.tenant_id == ctx.tenant_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={
            "error": {"code": "USER_INACTIVE", "message": "This account is inactive or no longer exists."}
        })
    return user


async def get_current_tenant(
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_async_db),
) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == ctx.tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
            "error": {"code": "TENANT_INACTIVE", "message": "This tenant is inactive."}
        })
    return tenant
