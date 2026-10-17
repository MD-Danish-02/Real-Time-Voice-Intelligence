import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.db.session import get_async_db
from app.schemas.auth import (
    AuthTokenResponse, LoginRequest, PasswordResetConfirm, PasswordResetMessage,
    PasswordResetRequest, RefreshRequest, SignupRequest, UserOut,
)
from app.models.user import PasswordResetToken, RefreshToken, User
from app.security.rate_limit import auth_rate_limit_dependency
from app.security.tenant import get_current_user
from app.services import audit_service, auth_service
from app.services.email_service import send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, token: str) -> None:
    options = {
        "key": settings.AUTH_REFRESH_COOKIE_NAME,
        "value": token,
        "httponly": True,
        "secure": settings.AUTH_REFRESH_COOKIE_SECURE,
        "samesite": settings.AUTH_REFRESH_COOKIE_SAMESITE,
        "max_age": settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        "path": f"{settings.API_V1_PREFIX}/auth",
    }
    if settings.AUTH_REFRESH_COOKIE_DOMAIN:
        options["domain"] = settings.AUTH_REFRESH_COOKIE_DOMAIN
    response.set_cookie(**options)


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.AUTH_REFRESH_COOKIE_NAME,
        path=f"{settings.API_V1_PREFIX}/auth",
        domain=settings.AUTH_REFRESH_COOKIE_DOMAIN or None,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


@router.post("/signup", response_model=AuthTokenResponse, dependencies=[Depends(auth_rate_limit_dependency)])
async def signup(payload: SignupRequest, request: Request, response: Response,
                 db: AsyncSession = Depends(get_async_db)):
    user, tenant = await auth_service.signup(db, payload)
    # issue tokens through the normal login path so the refresh token is registered server-side (rotation/revocation work)
    access, refresh = await auth_service.login(db, LoginRequest(email=payload.email, password=payload.password))
    await audit_service.record(db, tenant_id=tenant.id, actor_user_id=user.id, action="user.signup",
                               resource_type="tenant", resource_id=str(tenant.id),
                               request_id=getattr(request.state, "request_id", None))
    _set_refresh_cookie(response, refresh)
    return AuthTokenResponse(access_token=access)


@router.post("/login", response_model=AuthTokenResponse, dependencies=[Depends(auth_rate_limit_dependency)])
async def login(payload: LoginRequest, request: Request, response: Response,
                db: AsyncSession = Depends(get_async_db)):
    access, refresh = await auth_service.login(db, payload)
    from app.core.security import decode_token
    claims = decode_token(access)
    await audit_service.record(db, tenant_id=uuid.UUID(claims["tenant_id"]), actor_user_id=uuid.UUID(claims["sub"]),
                               action="user.login", resource_type="user", resource_id=claims["sub"],
                               request_id=getattr(request.state, "request_id", None))
    _set_refresh_cookie(response, refresh)
    return AuthTokenResponse(access_token=access)


@router.post(
    "/password-reset/request",
    response_model=PasswordResetMessage,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(auth_rate_limit_dependency)],
)
async def request_password_reset(
    payload: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
):
    # Keep the response identical for known and unknown email addresses.
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    delivery_mode = "email" if settings.SMTP_HOST and settings.SMTP_FROM_EMAIL else "development_log"

    if user is not None and user.is_active:
        now = datetime.now(timezone.utc)
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used.is_(False))
            .values(used=True)
        )
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        db.add(PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
        ))
        await db.commit()
        reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password#token={quote(token, safe='')}"
        background_tasks.add_task(send_password_reset_email, user.email, reset_url)

    return PasswordResetMessage(
        message="If an account exists for that email, password reset instructions are on the way.",
        delivery_mode=delivery_mode,
    )


@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetMessage,
    dependencies=[Depends(auth_rate_limit_dependency)],
)
async def confirm_password_reset(
    payload: PasswordResetConfirm,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    result = await db.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .with_for_update()
    )
    reset_token = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if reset_token is None or reset_token.used or reset_token.expires_at <= now:
        raise AppError("INVALID_OR_EXPIRED_RESET_TOKEN", "This password reset link is invalid or has expired.", status_code=400)

    user_result = await db.execute(select(User).where(User.id == reset_token.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AppError("INVALID_OR_EXPIRED_RESET_TOKEN", "This password reset link is invalid or has expired.", status_code=400)

    reset_token.used = True
    user.hashed_password = hash_password(payload.new_password)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
    await audit_service.record(
        db, tenant_id=user.tenant_id, actor_user_id=user.id,
        action="user.password_reset", resource_type="user", resource_id=str(user.id),
        request_id=getattr(request.state, "request_id", None),
    )
    await db.commit()
    return PasswordResetMessage(message="Your password has been reset. You can now sign in.", delivery_mode="complete")


@router.post("/refresh", response_model=AuthTokenResponse, dependencies=[Depends(auth_rate_limit_dependency)])
async def refresh_token(
    response: Response,
    payload: RefreshRequest | None = None,
    refresh_cookie: str | None = Cookie(default=None, alias=settings.AUTH_REFRESH_COOKIE_NAME),
    db: AsyncSession = Depends(get_async_db),
):
    refresh = (payload.refresh_token if payload else None) or refresh_cookie
    if not refresh:
        from app.core.errors import AppError
        raise AppError("MISSING_REFRESH_TOKEN", "A refresh session is required.", status_code=401)
    access, rotated_refresh = await auth_service.refresh(db, refresh)
    _set_refresh_cookie(response, rotated_refresh)
    return AuthTokenResponse(access_token=access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    payload: RefreshRequest | None = None,
    refresh_cookie: str | None = Cookie(default=None, alias=settings.AUTH_REFRESH_COOKIE_NAME),
    db: AsyncSession = Depends(get_async_db),
):
    refresh = (payload.refresh_token if payload else None) or refresh_cookie
    await auth_service.revoke_refresh_token(db, refresh)
    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return user
