import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import (
    create_access_token, create_refresh_token, decode_token, hash_password, verify_password,
)
from app.models.enums import UserRole
from app.models.tenant import Tenant, TenantBusinessRules
from app.models.user import RefreshToken, User
from app.schemas.auth import LoginRequest, SignupRequest


def _slugify(name: str) -> str:
    return "-".join(name.lower().split()) + "-" + uuid.uuid4().hex[:6]


async def signup(db: AsyncSession, payload: SignupRequest) -> tuple[User, Tenant]:
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise AppError("EMAIL_ALREADY_REGISTERED", "An account with this email already exists.", status_code=409)

    tenant = Tenant(name=payload.company_name, slug=_slugify(payload.company_name))
    db.add(tenant)
    await db.flush()

    rules = TenantBusinessRules(tenant_id=tenant.id)
    db.add(rules)

    user = User(
        tenant_id=tenant.id, email=payload.email, hashed_password=hash_password(payload.password),
        full_name=payload.full_name, role=UserRole.OWNER.value, is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await db.refresh(tenant)
    return user, tenant


async def login(db: AsyncSession, payload: LoginRequest) -> tuple[str, str]:
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise AppError("INVALID_CREDENTIALS", "Email or password is incorrect.", status_code=401)
    if not user.is_active:
        raise AppError("ACCOUNT_INACTIVE", "This account has been deactivated.", status_code=403)

    user.last_login_at = datetime.now(timezone.utc)
    jti = uuid.uuid4().hex
    refresh_token = create_refresh_token(user.id, user.tenant_id, jti)
    db.add(RefreshToken(
        user_id=user.id, tenant_id=user.tenant_id, jti=jti,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    ))
    await db.commit()

    access_token = create_access_token(user.id, user.tenant_id, user.role)
    return access_token, refresh_token


async def refresh(db: AsyncSession, refresh_token: str) -> tuple[str, str]:
    try:
        payload = decode_token(refresh_token)
    except Exception:
        raise AppError("INVALID_REFRESH_TOKEN", "Refresh token is invalid or expired.", status_code=401)
    if payload.get("type") != "refresh":
        raise AppError("WRONG_TOKEN_TYPE", "A refresh token is required.", status_code=401)

    jti = payload.get("jti")
    result = await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    stored = result.scalar_one_or_none()
    if stored is None or stored.revoked or stored.expires_at < datetime.now(timezone.utc):
        raise AppError("REFRESH_TOKEN_REVOKED", "This refresh token is no longer valid.", status_code=401)

    stored.revoked = True  # rotate on use
    user_result = await db.execute(select(User).where(User.id == stored.user_id))
    user = user_result.scalar_one()

    new_jti = uuid.uuid4().hex
    new_refresh = create_refresh_token(user.id, user.tenant_id, new_jti)
    db.add(RefreshToken(
        user_id=user.id, tenant_id=user.tenant_id, jti=new_jti,
        expires_at=datetime.now(timezone.utc) + timedelta(days=14),
    ))
    await db.commit()

    new_access = create_access_token(user.id, user.tenant_id, user.role)
    return new_access, new_refresh


async def revoke_refresh_token(db: AsyncSession, refresh_token: str | None) -> None:
    """Revoke one refresh session. Invalid or already-revoked tokens are idempotent."""
    if not refresh_token:
        return
    try:
        payload = decode_token(refresh_token)
    except Exception:  # noqa: BLE001 - logout should still clear the browser cookie
        return
    if payload.get("type") != "refresh" or not payload.get("jti"):
        return
    result = await db.execute(select(RefreshToken).where(RefreshToken.jti == payload["jti"]))
    stored = result.scalar_one_or_none()
    if stored is not None and not stored.revoked:
        stored.revoked = True
        await db.commit()
