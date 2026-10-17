import secrets
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.db.session import get_async_db
from app.models.user import APIKey
from app.security.rbac import require_admin
from app.security.tenant import RequestContext
from app.services import audit_service

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str


@router.get("")
async def list_keys(ctx: RequestContext = Depends(require_admin), db: AsyncSession = Depends(get_async_db)):
    rows = (await db.execute(select(APIKey).where(APIKey.tenant_id == ctx.tenant_id))).scalars().all()
    return [{"id": str(k.id), "name": k.name, "prefix": k.key_prefix, "revoked": k.revoked} for k in rows]


@router.post("")
async def create_key(payload: ApiKeyCreate, ctx: RequestContext = Depends(require_admin),
                     db: AsyncSession = Depends(get_async_db)):
    raw = "vx_" + secrets.token_urlsafe(32)
    key_row = APIKey(tenant_id=ctx.tenant_id, name=payload.name, key_prefix=raw[:10], hashed_key=hash_password(raw),
                     created_by_user_id=ctx.user_id)
    db.add(key_row)
    await db.commit()
    await db.refresh(key_row)
    await audit_service.record(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="api_key.created",
                               resource_type="api_key", resource_id=str(key_row.id), metadata={"name": payload.name})
    return {"key": raw, "note": "Store this now - it will not be shown again."}


@router.delete("/{key_id}")
async def revoke_key(key_id: uuid.UUID, ctx: RequestContext = Depends(require_admin),
                     db: AsyncSession = Depends(get_async_db)):
    key = (await db.execute(select(APIKey).where(APIKey.id == key_id, APIKey.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if key is None:
        raise AppError("KEY_NOT_FOUND", "API key not found.", status_code=404)
    key.revoked = True
    await db.commit()
    await audit_service.record(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="api_key.revoked",
                               resource_type="api_key", resource_id=str(key.id))
    return {"revoked": True}
