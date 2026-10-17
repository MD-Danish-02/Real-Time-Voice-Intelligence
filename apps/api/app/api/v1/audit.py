import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.models.user import AuditLog
from app.schemas.common import Page
from app.security.rbac import require_admin
from app.security.tenant import RequestContext

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_user_id: Optional[uuid.UUID]
    action: str
    resource_type: str
    resource_id: Optional[str]
    request_id: Optional[str]
    metadata_json: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=Page[AuditLogOut])
async def list_audit_logs(action: Optional[str] = None, resource_type: Optional[str] = None,
                          page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                          ctx: RequestContext = Depends(require_admin), db: AsyncSession = Depends(get_async_db)):
    stmt = select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(AuditLog.created_at.desc())
                              .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return Page[AuditLogOut](items=items, total=total, page=page, page_size=page_size)
