from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.models.tenant import TenantBusinessRules
from app.models.knowledge import KnowledgeDocument
from app.models.crm import CRMConnection
from app.models.lead import Lead
from app.schemas.tenant import BusinessRulesOut, BusinessRulesUpdate
from app.security.rbac import require_admin, require_any_role
from app.security.tenant import RequestContext, get_current_tenant
from app.services import audit_service

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me")
async def my_tenant(tenant=Depends(get_current_tenant)):
    return {"id": str(tenant.id), "name": tenant.name, "plan": tenant.plan}


@router.get("/business-rules", response_model=BusinessRulesOut)
async def get_rules(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    return (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == ctx.tenant_id))).scalar_one()


@router.put("/business-rules", response_model=BusinessRulesOut)
async def update_rules(payload: BusinessRulesUpdate, ctx: RequestContext = Depends(require_admin),
                       db: AsyncSession = Depends(get_async_db)):
    rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == ctx.tenant_id))).scalar_one()
    changed = payload.model_dump(exclude_unset=True)
    for k, v in changed.items():
        setattr(rules, k, v)
    await db.commit()
    await db.refresh(rules)
    await audit_service.record(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="integration.changed",
                               resource_type="business_rules", metadata=changed)
    return rules


@router.get("/ai-status", summary="Real setup/AI readiness derived from the database")
async def ai_status(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    healthy_crm_statuses = ("connected", "synced")
    crm_count = (await db.execute(select(func.count()).select_from(CRMConnection).where(
        CRMConnection.tenant_id == ctx.tenant_id, CRMConnection.status.in_(healthy_crm_statuses)
    ))).scalar_one()
    lead_count = (await db.execute(select(func.count()).select_from(Lead).where(
        Lead.tenant_id == ctx.tenant_id
    ))).scalar_one()
    knowledge_count = (await db.execute(select(func.count()).select_from(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == ctx.tenant_id, KnowledgeDocument.status == "processed"
    ))).scalar_one()
    rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == ctx.tenant_id))).scalar_one()
    return {"crm_connected": bool(crm_count), "lead_count": lead_count, "knowledge_count": knowledge_count,
            "knowledge_ready": knowledge_count > 0,
            "ai_ready": bool(crm_count and knowledge_count),
            "auto_call_enabled": rules.auto_call_enabled}
