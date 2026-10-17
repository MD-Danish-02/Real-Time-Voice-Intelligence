"""
Audit logging. Call `record` after an action succeeds (not before), so
the log reflects what actually happened. Never pass secrets in metadata
- it is stored as plain JSON text and returned to admins verbatim.
"""
import json
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import AuditLog

_DENYLIST = {"password", "token", "access_token", "refresh_token", "api_key", "secret", "hashed_password"}


def _scrub(metadata: Optional[dict]) -> Optional[dict]:
    if not metadata:
        return metadata
    return {k: ("***" if k.lower() in _DENYLIST else v) for k, v in metadata.items()}


async def record(
    db: AsyncSession, *, tenant_id: uuid.UUID, action: str, resource_type: str,
    actor_user_id: Optional[uuid.UUID] = None, resource_id: Optional[str] = None,
    request_id: Optional[str] = None, metadata: Optional[dict] = None,
) -> None:
    db.add(AuditLog(
        tenant_id=tenant_id, actor_user_id=actor_user_id, action=action, resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None, request_id=request_id,
        metadata_json=json.dumps(_scrub(metadata), default=str)[:4000] if metadata else None,
    ))
    await db.commit()
