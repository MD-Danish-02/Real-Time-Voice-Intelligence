"""
Password hashing (Argon2id) and JWT access/refresh token helpers.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHash
from jose import jwt

from app.core.config import settings

_hasher = PasswordHasher()


def hash_password(raw_password: str) -> str:
    return _hasher.hash(raw_password)


def verify_password(raw_password: str, hashed_password: str) -> bool:
    try:
        return _hasher.verify(hashed_password, raw_password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def needs_rehash(hashed_password: str) -> bool:
    return _hasher.check_needs_rehash(hashed_password)


def _create_token(subject: str, tenant_id: Optional[str], role: Optional[str],
                   expires_delta: timedelta, token_type: str, extra: Optional[dict] = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    if tenant_id:
        payload["tenant_id"] = tenant_id
    if role:
        payload["role"] = role
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: UUID, tenant_id: UUID, role: str) -> str:
    return _create_token(
        subject=str(user_id),
        tenant_id=str(tenant_id),
        role=role,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type="access",
    )


def create_refresh_token(user_id: UUID, tenant_id: UUID, jti: str) -> str:
    return _create_token(
        subject=str(user_id),
        tenant_id=str(tenant_id),
        role=None,
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        token_type="refresh",
        extra={"jti": jti},
    )


def decode_token(token: str) -> dict:
    """Raises JWTError on invalid/expired token."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


class TokenError(Exception):
    pass
