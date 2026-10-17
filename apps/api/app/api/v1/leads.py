import uuid
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.session import get_async_db
from app.models.lead import Lead, LeadCallState
from app.models.tenant import Tenant, TenantBusinessRules
from app.schemas.common import Page
from app.schemas.lead import LeadCallStateOut, LeadDetailOut, LeadOut
from app.security.rbac import require_manager, require_any_role
from app.security.tenant import RequestContext
from app.services.eligibility_service import check_eligibility, enqueue_call_if_eligible

router = APIRouter(prefix="/leads", tags=["leads"])

SORTABLE = {"created_at": Lead.created_at, "name": Lead.name, "crm_status": Lead.crm_status}


@router.get("", response_model=Page[LeadOut])
async def list_leads(q: Optional[str] = None, crm_status: Optional[str] = None, page: int = Query(1, ge=1),
                     page_size: int = Query(25, ge=1, le=100), sort: str = "created_at", desc: bool = True,
                     ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    stmt = select(Lead).where(Lead.tenant_id == ctx.tenant_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Lead.name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like),
                              Lead.company.ilike(like)))
    if crm_status:
        stmt = stmt.where(Lead.crm_status == crm_status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    col = SORTABLE.get(sort, Lead.created_at)
    stmt = stmt.order_by(col.desc() if desc else col.asc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()
    state_rows = await db.execute(select(LeadCallState).where(
        LeadCallState.tenant_id == ctx.tenant_id, LeadCallState.lead_id.in_([lead.id for lead in items])
    )) if items else None
    states = {state.lead_id: state for state in state_rows.scalars().all()} if state_rows else {}
    output = []
    for lead in items:
        state = states.get(lead.id)
        row = LeadOut.model_validate(lead).model_dump()
        row.update({
            "source_type": lead.crm_provider,
            "custom_fields": json.loads(lead.custom_fields_json or "{}"),
            "calling_status": state.calling_status if state else "not_called",
            "attempt_count": state.attempt_count if state else 0,
            "do_not_call": state.do_not_call if state else False,
            "eligible": state.eligible if state else False,
        })
        output.append(row)
    return Page[LeadOut](items=output, total=total, page=page, page_size=page_size)


async def _load(db, ctx, lead_id):
    lead = (await db.execute(select(Lead).where(Lead.id == lead_id, Lead.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "Lead not found.", status_code=404)
    return lead


@router.get("/{lead_id}", response_model=LeadDetailOut)
async def get_lead(lead_id: uuid.UUID, ctx: RequestContext = Depends(require_any_role),
                   db: AsyncSession = Depends(get_async_db)):
    lead = await _load(db, ctx, lead_id)
    state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id,
                                                          LeadCallState.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    out = LeadDetailOut.model_validate(lead)
    out.custom_fields = json.loads(lead.custom_fields_json or "{}")
    out.call_state = LeadCallStateOut.model_validate(state) if state else None
    return out


@router.get("/{lead_id}/eligibility")
async def lead_eligibility(lead_id: uuid.UUID, ctx: RequestContext = Depends(require_any_role),
                           db: AsyncSession = Depends(get_async_db)):
    lead = await _load(db, ctx, lead_id)
    tenant = (await db.execute(select(Tenant).where(Tenant.id == ctx.tenant_id))).scalar_one()
    rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == ctx.tenant_id))).scalar_one()
    state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
    r = await check_eligibility(db, tenant, rules, lead, state)
    return {"eligible": r.eligible, "reason": r.reason, "next_action": r.next_action}


@router.post("/{lead_id}/call", summary="Queue an AI call (still subject to eligibility)")
async def call_lead(lead_id: uuid.UUID, ctx: RequestContext = Depends(require_manager),
                    db: AsyncSession = Depends(get_async_db)):
    lead = await _load(db, ctx, lead_id)
    tenant = (await db.execute(select(Tenant).where(Tenant.id == ctx.tenant_id))).scalar_one()
    rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == ctx.tenant_id))).scalar_one()
    state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
    queued, reason = await enqueue_call_if_eligible(db, tenant, rules, lead, state, reason_hint="manual")
    return {"queued": queued, "reason": reason}


@router.post("/{lead_id}/do-not-call")
async def set_dnc(lead_id: uuid.UUID, value: bool = True, ctx: RequestContext = Depends(require_manager),
                  db: AsyncSession = Depends(get_async_db)):
    lead = await _load(db, ctx, lead_id)
    state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
    state.manual_do_not_call = value
    from app.services.dnc_service import apply_dnc_state
    await apply_dnc_state(db, ctx.tenant_id, lead, state)
    await db.commit()
    return {"do_not_call": state.do_not_call, "manual_do_not_call": state.manual_do_not_call}
